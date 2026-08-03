from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from filelock import FileLock


TRANSACTION_FIELDS = [
    "transaction_id",
    "executed_at",
    "symbol",
    "security_name",
    "broker_name",
    "action",
    "price",
    "quantity",
    "gross_amount",
    "currency",
    "source",
    "note",
    "imported_at",
]
VALID_ACTIONS = {"buy", "sell", "dividend", "account_designation"}
SHANGHAI_TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value or "0").replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"invalid {field}: {value}") from exc
    if parsed < 0:
        raise ValueError(f"{field} cannot be negative")
    return parsed


def _decimal_text(value: Decimal, places: str = "0.001") -> str:
    return format(value.quantize(Decimal(places)), "f")


def _normalise_time(value: str) -> str:
    text = str(value or "").strip().replace(" ", "T")
    if not text:
        raise ValueError("executed_at is required")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid executed_at: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.isoformat(timespec="seconds")


@dataclass(frozen=True)
class PortfolioTransaction:
    transaction_id: str
    executed_at: str
    symbol: str
    security_name: str
    broker_name: str
    action: str
    price: str
    quantity: int
    gross_amount: str
    currency: str
    source: str
    note: str
    imported_at: str

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "PortfolioTransaction":
        action = str(value.get("action") or "").strip().lower()
        if action not in VALID_ACTIONS:
            raise ValueError(f"unsupported portfolio action: {action}")
        symbol = str(value.get("symbol") or "").strip()
        if action != "account_designation" and (len(symbol) != 6 or not symbol.isdigit()):
            raise ValueError(f"invalid symbol for {action}: {symbol}")
        if action == "account_designation":
            symbol = ""
        executed_at = _normalise_time(str(value.get("executed_at") or ""))
        price = _decimal(value.get("price"), "price")
        quantity = int(value.get("quantity") or 0)
        gross_amount = _decimal(value.get("gross_amount"), "gross_amount")
        if action in {"buy", "sell"} and quantity <= 0:
            raise ValueError(f"quantity must be positive for {action}")
        if action == "dividend" and quantity != 0:
            raise ValueError("quantity must be zero for dividend")
        if action == "account_designation" and quantity < 0:
            raise ValueError("quantity cannot be negative for account_designation")
        if action in {"buy", "sell"} and gross_amount == 0:
            gross_amount = price * quantity
        identity = "|".join(
            [executed_at, symbol, action, _decimal_text(price), str(quantity), _decimal_text(gross_amount)]
        )
        transaction_id = str(value.get("transaction_id") or "").strip()
        if not transaction_id:
            transaction_id = "TX-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return cls(
            transaction_id=transaction_id,
            executed_at=executed_at,
            symbol=symbol,
            security_name=str(value.get("security_name") or "").strip(),
            broker_name=str(value.get("broker_name") or value.get("security_name") or "").strip(),
            action=action,
            price=_decimal_text(price),
            quantity=quantity,
            gross_amount=_decimal_text(gross_amount),
            currency=str(value.get("currency") or "CNY").strip().upper(),
            source=str(value.get("source") or "manual").strip(),
            note=str(value.get("note") or "").strip(),
            imported_at=str(value.get("imported_at") or now_iso()).strip(),
        )


class PortfolioStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "transactions.csv"
        self.backup_root = self.root / "backups"
        self.lock_path = self.root / ".portfolio.lock"

    def load(self) -> list[PortfolioTransaction]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [PortfolioTransaction.from_mapping(row) for row in csv.DictReader(handle)]

    def record_many(self, values: Iterable[PortfolioTransaction]) -> dict[str, Any]:
        incoming = list(values)
        with FileLock(str(self.lock_path), timeout=30):
            existing = self.load()
            by_id = {item.transaction_id: item for item in existing}
            created = 0
            duplicate = 0
            for item in incoming:
                current = by_id.get(item.transaction_id)
                if current is not None:
                    if current != item:
                        left = asdict(current)
                        right = asdict(item)
                        left.pop("imported_at", None)
                        right.pop("imported_at", None)
                        if left != right:
                            raise ValueError(f"transaction id conflict: {item.transaction_id}")
                    duplicate += 1
                    continue
                by_id[item.transaction_id] = item
                created += 1
            if created:
                if self.path.exists():
                    self.backup_root.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                    shutil.copy2(self.path, self.backup_root / f"{stamp}_transactions.csv")
                ordered = sorted(by_id.values(), key=lambda item: (item.executed_at, item.transaction_id))
                temp_path = self.path.with_suffix(".csv.tmp")
                with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=TRANSACTION_FIELDS)
                    writer.writeheader()
                    for item in ordered:
                        writer.writerow(asdict(item))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self.path)
        return {"created": created, "duplicate": duplicate, "total": len(by_id)}

    def summary(self) -> dict[str, Any]:
        states: dict[str, dict[str, Any]] = {}
        warnings: list[str] = []
        for item in sorted(self.load(), key=lambda row: (row.executed_at, row.transaction_id)):
            if not item.symbol:
                continue
            state = states.setdefault(
                item.symbol,
                {
                    "symbol": item.symbol,
                    "security_name": item.security_name,
                    "quantity": 0,
                    "cost_basis": Decimal("0"),
                    "realized_trading_pnl": Decimal("0"),
                    "dividends": Decimal("0"),
                    "cash_flow": Decimal("0"),
                    "first_transaction_at": item.executed_at,
                    "last_transaction_at": item.executed_at,
                },
            )
            state["security_name"] = item.security_name or state["security_name"]
            state["last_transaction_at"] = item.executed_at
            amount = Decimal(item.gross_amount)
            if item.action == "buy":
                state["quantity"] += item.quantity
                state["cost_basis"] += amount
                state["cash_flow"] -= amount
            elif item.action == "sell":
                prior_quantity = state["quantity"]
                if prior_quantity < item.quantity:
                    warnings.append(
                        f"{item.transaction_id}: sell quantity exceeds recorded position; history may be incomplete"
                    )
                    average_cost = Decimal("0")
                else:
                    average_cost = state["cost_basis"] / prior_quantity if prior_quantity else Decimal("0")
                removed_cost = average_cost * item.quantity
                state["realized_trading_pnl"] += amount - removed_cost
                state["quantity"] -= item.quantity
                state["cost_basis"] = max(Decimal("0"), state["cost_basis"] - removed_cost)
            elif item.action == "dividend":
                state["dividends"] += amount
                state["cash_flow"] += amount
                continue
            if item.action == "sell":
                state["cash_flow"] += amount

        positions: list[dict[str, Any]] = []
        total_open_cost = Decimal("0")
        total_realized = Decimal("0")
        total_dividends = Decimal("0")
        total_cash_flow = Decimal("0")
        for symbol in sorted(states):
            state = states[symbol]
            quantity = int(state["quantity"])
            cost_basis = Decimal(state["cost_basis"])
            average_cost = cost_basis / quantity if quantity > 0 else Decimal("0")
            realized_trading = Decimal(state["realized_trading_pnl"])
            dividends = Decimal(state["dividends"])
            total_open_cost += cost_basis
            total_realized += realized_trading
            total_dividends += dividends
            total_cash_flow += Decimal(state["cash_flow"])
            positions.append(
                {
                    "symbol": symbol,
                    "security_name": state["security_name"],
                    "quantity": quantity,
                    "status": "open" if quantity else "closed",
                    "average_cost": _decimal_text(average_cost),
                    "open_cost": _decimal_text(cost_basis),
                    "realized_trading_pnl": _decimal_text(realized_trading),
                    "dividends": _decimal_text(dividends),
                    "realized_total_pnl": _decimal_text(realized_trading + dividends),
                    "cash_flow": _decimal_text(Decimal(state["cash_flow"])),
                    "first_transaction_at": state["first_transaction_at"],
                    "last_transaction_at": state["last_transaction_at"],
                }
            )
        return {
            "transactions": len(self.load()),
            "positions": positions,
            "open_positions": sum(1 for item in positions if item["status"] == "open"),
            "open_cost": _decimal_text(total_open_cost),
            "realized_trading_pnl": _decimal_text(total_realized),
            "dividends": _decimal_text(total_dividends),
            "realized_total_pnl": _decimal_text(total_realized + total_dividends),
            "net_cash_flow": _decimal_text(total_cash_flow),
            "fees_included": False,
            "warnings": warnings,
        }


def load_transactions_json(path: Path) -> list[PortfolioTransaction]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    rows = payload.get("transactions", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("portfolio import JSON must be a list or contain a transactions list")
    return [PortfolioTransaction.from_mapping(dict(row)) for row in rows]
