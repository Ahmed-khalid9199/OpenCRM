"""Repository and compatibility layer for the consolidated CRM data access.

The application historically used sqlite3 connections directly in both
``database.py`` and ``dashboard.py``.  This module keeps those call sites
working while routing every query through the single SQLAlchemy engine and
session factory in :mod:`db.session`.

New code should prefer the explicit domain methods on :class:`CRMRepository`.
``SQLAlchemyConnection`` exists only to migrate the existing dashboard routes
without changing their business logic.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, select, text, update, delete
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .models import Campaign, CampaignSend, Lead, LeadNote, Sequence, SequenceEnrollment, User
from .session import get_engine, get_session, mask_database_url, remove_session

logger = logging.getLogger(__name__)


class DatabaseError(Exception):
    """Custom exception for database-related errors."""


_LEAD_STRING_FIELDS = (
    "form_id",
    "email",
    "full_name",
    "first_name",
    "last_name",
    "phone_number",
    "company_name",
    "job_title",
    "status",
    "lead_source",
    "assigned_to",
)

_LEAD_TIMESTAMP_FIELDS = (
    "email_sent_at",
    "inserted_at",
    "updated_at",
    "follow_up_date",
    "expected_close_date",
)


def _as_datetime(value: Any) -> Any:
    """Coerce legacy timestamp strings into timezone-aware datetimes."""
    if value is None or isinstance(value, datetime):
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return value


def _as_decimal(value: Any) -> Decimal:
    """Coerce numeric input into a value suitable for NUMERIC(14, 2)."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception as exc:  # decimal.InvalidOperation and TypeError/ValueError
        raise ValueError(f"invalid decimal value: {value!r}") from exc


def _as_json(value: Any, default: Any) -> Any:
    """Return a JSONB-compatible value from dict/list or JSON text."""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return default
        if isinstance(parsed, (dict, list)):
            return parsed
    return default


def _serialize(value: Any) -> Any:
    """Convert PostgreSQL values back to the shapes legacy callers expect."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value


def _row_to_dict(row: Any) -> Dict[str, Any]:
    mapping = row._mapping if hasattr(row, "_mapping") else row
    return {key: _serialize(mapping[key]) for key in mapping.keys()}


class RowAdapter:
    """sqlite3.Row-compatible wrapper around a SQLAlchemy result row."""

    def __init__(self, keys: Sequence[str], values: Sequence[Any]) -> None:
        self._keys = tuple(keys)
        # Dashboard routes were written against SQLite TEXT JSON columns and
        # frequently call json.loads() on these values.  Keep that contract.
        self._values = tuple(
            json.dumps(value, default=str) if isinstance(value, (dict, list)) else _serialize(value)
            for value in values
        )
        self._mapping = {key: value for key, value in zip(self._keys, self._values)}

    def keys(self) -> Tuple[str, ...]:
        return self._keys

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._mapping[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._mapping.get(key, default)

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"RowAdapter({self._mapping!r})"


class ResultAdapter:
    """Small sqlite3-cursor-like adapter over an eagerly fetched result."""

    def __init__(self, result: Any, lastrowid: Optional[int] = None) -> None:
        returns_rows = result is not None and getattr(result, "returns_rows", False)
        keys = list(result.keys()) if returns_rows else []
        rows = result.fetchall() if returns_rows else []
        self._rows = [RowAdapter(keys, row) for row in rows]
        self._index = 0
        self.rowcount = result.rowcount if result is not None else -1
        self.lastrowid = lastrowid
        self.description = [(key, None, None, None, None, None, None) for key in keys]

    def fetchone(self) -> Optional[RowAdapter]:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchmany(self, size: int = 1) -> List[RowAdapter]:
        rows = self._rows[self._index : self._index + size]
        self._index += len(rows)
        return rows

    def fetchall(self) -> List[RowAdapter]:
        rows = self._rows[self._index :]
        self._index = len(self._rows)
        return rows

    def scalar(self) -> Any:
        row = self.fetchone()
        return row[0] if row is not None else None

    def close(self) -> None:
        return None


_PLACEHOLDER_RE = re.compile(r"\?")


def _replace_placeholders(sql: str, params: Sequence[Any]) -> Tuple[str, Dict[str, Any]]:
    """Translate ``?`` placeholders to named SQLAlchemy parameters."""
    if not params:
        return sql, {}

    named: Dict[str, Any] = {}
    chunks: List[str] = []
    param_index = 0
    in_single_quote = False
    in_double_quote = False
    i = 0

    while i < len(sql):
        char = sql[i]
        if char == "'" and not in_double_quote:
            if in_single_quote and i + 1 < len(sql) and sql[i + 1] == "'":
                chunks.append("''")
                i += 2
                continue
            in_single_quote = not in_single_quote
        elif char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote

        if char == "?" and not in_single_quote and not in_double_quote:
            if param_index >= len(params):
                raise DatabaseError("More ? placeholders than supplied parameters")
            name = f"p{param_index}"
            named[name] = params[param_index]
            chunks.append(f":{name}")
            param_index += 1
        else:
            chunks.append(char)
        i += 1

    if param_index != len(params):
        raise DatabaseError("More parameters supplied than ? placeholders")
    return "".join(chunks), named


def _translate_sqlite_sql(sql: str) -> str:
    """Translate the handful of SQLite constructs used by the legacy app."""
    translated = sql

    translated = re.sub(
        r"datetime\s*\(\s*'now'\s*,\s*'(-?\d+)\s+(day|hour|minute)s?'\s*\)",
        lambda m: f"(CURRENT_TIMESTAMP - INTERVAL '{abs(int(m.group(1)))} {m.group(2)}')",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"datetime\s*\(\s*'now'\s*\)",
        "CURRENT_TIMESTAMP",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"strftime\s*\(\s*'%Y-%m-%d %H:%M:%S'\s*,\s*([^)]+)\)",
        r"to_char(\1 AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"strftime\s*\(\s*'%Y-%m-%d'\s*,\s*([^)]+)\)",
        r"to_char(\1 AT TIME ZONE 'UTC', 'YYYY-MM-DD')",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"strftime\s*\(\s*'%Y-%m'\s*,\s*([^)]+)\)",
        r"to_char(\1 AT TIME ZONE 'UTC', 'YYYY-MM')",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"datetime\s*\(\s*([^)'\"][^)]*)\s*\)",
        r"\1",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"json_extract\s*\(\s*([^,]+)\s*,\s*'\$\.([^']+)'\s*\)",
        r"(\1 ->> '\2')",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"date\s*\(\s*'now'\s*,\s*'(-?\d+)\s+days?'\s*\)",
        lambda m: f"(CURRENT_DATE - INTERVAL '{abs(int(m.group(1)))} days')",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"date\s*\(\s*'now'\s*\)",
        "CURRENT_DATE",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"date\s*\(\s*([^)'\"][^)]*)\s*\)",
        r"((\1 AT TIME ZONE 'UTC')::date)",
        translated,
        flags=re.IGNORECASE,
    )
    # SQLite LIKE is case-insensitive for ASCII; PostgreSQL ILIKE preserves that.
    translated = re.sub(
        r"\bLIKE\s+(\?|:\w+)", r"ILIKE \1", translated, flags=re.IGNORECASE
    )
    translated = re.sub(
        r"\bGROUP_CONCAT\s*\(", "STRING_AGG(", translated, flags=re.IGNORECASE
    )
    translated = re.sub(
        r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", translated, flags=re.IGNORECASE
    )
    translated = re.sub(
        r"\bINSERT\s+OR\s+REPLACE\s+INTO\b", "INSERT INTO", translated, flags=re.IGNORECASE
    )
    return translated


def _ensure_returning_id(sql: str) -> Tuple[str, bool]:
    """Add RETURNING id to INSERT statements so lastrowid keeps working."""
    if not re.match(r"^\s*INSERT\b", sql, flags=re.IGNORECASE):
        return sql, False
    if re.search(r"\bRETURNING\b", sql, flags=re.IGNORECASE):
        return sql, True
    stripped = sql.rstrip().rstrip(";")
    return stripped + " RETURNING id", True


class SQLAlchemyConnection:
    """Minimal sqlite3-connection facade backed by a scoped SQLAlchemy session."""

    def __init__(self) -> None:
        self._session = get_session()
        self._last_result: Optional[ResultAdapter] = None

    def execute(self, sql: Any, params: Any = ()) -> ResultAdapter:
        sql_text = str(sql)
        sql_text = _translate_sqlite_sql(sql_text)

        if isinstance(params, Mapping):
            bound: Dict[str, Any] = dict(params)
            prepared_sql = sql_text
        else:
            prepared_sql, bound = _replace_placeholders(sql_text, tuple(params))

        wants_id = False
        if re.match(r"^\s*INSERT\b", prepared_sql, flags=re.IGNORECASE):
            prepared_sql, wants_id = _ensure_returning_id(prepared_sql)

        try:
            result = self._session.execute(text(prepared_sql), bound)
            lastrowid = None
            if wants_id and result.returns_rows:
                first = result.fetchone()
                if first is not None:
                    lastrowid = first[0]
                adapter = ResultAdapter(result, lastrowid)
                # Eager fetch consumed the RETURNING row; expose it for fetchone().
                if lastrowid is not None:
                    adapter._rows.insert(0, RowAdapter(result.keys(), (lastrowid,)))
            else:
                adapter = ResultAdapter(result, lastrowid)
            self._last_result = adapter
            return adapter
        except SQLAlchemyError as exc:
            self._session.rollback()
            logger.error("Database operation failed: %s", exc)
            raise DatabaseError(f"Database operation failed: {exc}") from exc

    executemany = execute

    def cursor(self) -> "SQLAlchemyConnection":
        """Return a cursor-like object. The facade itself is the cursor."""
        return self

    def fetchone(self) -> Optional[RowAdapter]:
        if self._last_result is None:
            return None
        return self._last_result.fetchone()

    def fetchmany(self, size: int = 1) -> List[RowAdapter]:
        if self._last_result is None:
            return []
        return self._last_result.fetchmany(size)

    def fetchall(self) -> List[RowAdapter]:
        if self._last_result is None:
            return []
        return self._last_result.fetchall()

    @property
    def rowcount(self) -> int:
        return self._last_result.rowcount if self._last_result else -1

    @property
    def lastrowid(self) -> Optional[int]:
        return self._last_result.lastrowid if self._last_result else None

    @property
    def description(self) -> Any:
        return self._last_result.description if self._last_result else None

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()

    def close(self) -> None:
        remove_session()

    def __enter__(self) -> "SQLAlchemyConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()


class CRMRepository:
    """Domain-level operations used by the pipeline and health checks."""

    def __init__(self) -> None:
        get_engine()

    @property
    def db_path(self) -> str:
        """Legacy attribute retained for logging compatibility."""
        from .session import get_database_url

        return mask_database_url(get_database_url())

    def connection(self) -> SQLAlchemyConnection:
        return SQLAlchemyConnection()

    def insert_lead(self, lead_data: Dict[str, Any]) -> Optional[int]:
        for field in ("lead_id", "created_time", "raw_field_data"):
            if field not in lead_data:
                raise DatabaseError(f"Missing required field: {field}")

        values: Dict[str, Any] = {
            "lead_id": lead_data["lead_id"],
            "created_time": _as_datetime(lead_data["created_time"]),
            "raw_field_data": _as_json(lead_data["raw_field_data"], {}),
            "tags": _as_json(lead_data.get("tags"), {}),
        }
        for field in _LEAD_STRING_FIELDS:
            if field in lead_data:
                values[field] = lead_data[field]
        for field in _LEAD_TIMESTAMP_FIELDS:
            if field in lead_data and lead_data[field] not in (None, ""):
                parsed = _as_datetime(lead_data[field])
                if parsed is None:
                    raise DatabaseError(f"{field} is not a valid timestamp")
                values[field] = parsed
        if "deal_value" in lead_data and lead_data["deal_value"] not in (None, ""):
            try:
                values["deal_value"] = _as_decimal(lead_data["deal_value"])
            except ValueError as exc:
                raise DatabaseError(str(exc)) from exc
        if values["created_time"] is None:
            raise DatabaseError("created_time is not a valid timestamp")

        session = get_session()
        try:
            result = session.execute(
                Lead.__table__.insert().values(**values).returning(Lead.id)
            )
            session.commit()
            new_id = result.scalar_one()
            logger.info("Inserted lead: %s (DB ID: %s)", values["lead_id"], new_id)
            return int(new_id)
        except IntegrityError:
            session.rollback()
            logger.warning("Lead already exists: %s", values["lead_id"])
            return None
        except SQLAlchemyError as exc:
            session.rollback()
            logger.error("Failed to insert lead: %s", exc)
            raise DatabaseError(f"Failed to insert lead: {exc}") from exc
        finally:
            remove_session()

    def lead_exists(self, lead_id: str) -> bool:
        session = get_session()
        try:
            return session.execute(
                select(func.count()).select_from(Lead).where(Lead.lead_id == lead_id)
            ).scalar_one() > 0
        except SQLAlchemyError as exc:
            logger.error("Failed to check lead existence: %s", exc)
            return False
        finally:
            remove_session()

    def update_lead_status(
        self,
        lead_id: str,
        status: str,
        email_sent_at: Optional[str] = None,
    ) -> bool:
        values: Dict[str, Any] = {
            "status": status,
            "updated_at": func.CURRENT_TIMESTAMP(),
        }
        if email_sent_at:
            values["email_sent_at"] = _as_datetime(email_sent_at)

        session = get_session()
        try:
            result = session.execute(
                update(Lead).where(Lead.lead_id == lead_id).values(**values)
            )
            session.commit()
            if result.rowcount and result.rowcount > 0:
                logger.info("Updated lead %s status to: %s", lead_id, status)
                return True
            logger.warning("No lead found with ID: %s", lead_id)
            return False
        except SQLAlchemyError as exc:
            session.rollback()
            logger.error("Failed to update lead status: %s", exc)
            return False
        finally:
            remove_session()

    def get_leads_by_status(
        self, status: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        statement = (
            select(Lead.__table__)
            .where(Lead.status == status)
            .order_by(Lead.created_time.desc())
        )
        if limit:
            statement = statement.limit(limit)
        session = get_session()
        try:
            return [_row_to_dict(row) for row in session.execute(statement).all()]
        except SQLAlchemyError as exc:
            logger.error("Failed to retrieve leads: %s", exc)
            return []
        finally:
            remove_session()

    def get_lead_by_id(self, lead_id: str) -> Optional[Dict[str, Any]]:
        session = get_session()
        try:
            row = session.execute(
                select(Lead.__table__).where(Lead.lead_id == lead_id)
            ).first()
            return _row_to_dict(row) if row is not None else None
        except SQLAlchemyError as exc:
            logger.error("Failed to retrieve lead: %s", exc)
            return None
        finally:
            remove_session()

    def get_all_leads(
        self, limit: Optional[int] = None, offset: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        statement = select(Lead.__table__).order_by(Lead.created_time.desc())
        if limit:
            statement = statement.limit(limit)
        if offset:
            statement = statement.offset(offset)
        session = get_session()
        try:
            return [_row_to_dict(row) for row in session.execute(statement).all()]
        except SQLAlchemyError as exc:
            logger.error("Failed to retrieve all leads: %s", exc)
            return []
        finally:
            remove_session()

    def get_lead_count(self, status: Optional[str] = None) -> int:
        statement = select(func.count()).select_from(Lead)
        if status:
            statement = statement.where(Lead.status == status)
        session = get_session()
        try:
            return int(session.execute(statement).scalar_one())
        except SQLAlchemyError as exc:
            logger.error("Failed to get lead count: %s", exc)
            return 0
        finally:
            remove_session()

    def delete_lead(self, lead_id: str) -> bool:
        session = get_session()
        try:
            result = session.execute(delete(Lead).where(Lead.lead_id == lead_id))
            session.commit()
            if result.rowcount and result.rowcount > 0:
                logger.info("Deleted lead: %s", lead_id)
                return True
            logger.warning("No lead found with ID: %s", lead_id)
            return False
        except SQLAlchemyError as exc:
            session.rollback()
            logger.error("Failed to delete lead: %s", exc)
            return False
        finally:
            remove_session()

    def get_stats(self) -> Dict[str, Any]:
        session = get_session()
        try:
            total = session.execute(
                select(func.count()).select_from(Lead)
            ).scalar_one()
            status_rows = session.execute(
                select(Lead.status, func.count()).group_by(Lead.status)
            ).all()
            recent = session.execute(
                select(func.count())
                .select_from(Lead)
                .where(Lead.inserted_at >= func.now() - text("INTERVAL '1 day'"))
            ).scalar_one()
            return {
                "total_leads": int(total),
                "status_breakdown": {row[0]: int(row[1]) for row in status_rows},
                "recent_24h": int(recent),
                "database_path": self.db_path,
            }
        except SQLAlchemyError as exc:
            logger.error("Failed to get database stats: %s", exc)
            return {"error": str(exc)}
        finally:
            remove_session()

    def vacuum(self) -> bool:
        try:
            with get_engine().connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as connection:
                connection.exec_driver_sql("VACUUM")
            logger.info("Database vacuumed successfully")
            return True
        except SQLAlchemyError as exc:
            logger.error("Failed to vacuum database: %s", exc)
            return False

    def close(self) -> None:
        remove_session()

    def __enter__(self) -> "CRMRepository":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
