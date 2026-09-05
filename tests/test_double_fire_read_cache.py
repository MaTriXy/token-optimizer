"""Double-fire read-cache damage: a first-ever Read can be denied if the
PreToolUse/Read hook fires twice for the same tool call. The read cache has
no tool_use_id idempotency, so the second fire sees the entry created by the
first and treats it as a redundant reread.

This is the exact user-visible damage from the double-hook registration bug:
the read cache denied a genuine FIRST read as redundant because hook fire #1
created the entry and hook fire #2 saw it.

Run: python3 -m pytest tests/test_double_fire_read_cache.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
READ_CACHE = SCRIPTS / "read_cache.py"
SESSION = "22222222-2222-2222-2222-222222222222"

# A realistic Python file that passes the structure_map's generated_like
# check (varied content, real-looking names, no repetitive boilerplate).
_REAL_PYTHON = '''#!/usr/bin/env python3
"""A module for processing user accounts and transactions.

This module provides classes and functions for managing user accounts,
processing financial transactions, and generating reports. It is designed
to be used in a banking or fintech application context.
"""

from __future__ import annotations

import os
import sys
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum, auto
from pathlib import Path
from typing import Any, Optional, Union
from collections import defaultdict, deque
from urllib.parse import urlparse, urljoin

logger = logging.getLogger(__name__)


class TransactionType(Enum):
    """Types of financial transactions supported by the system."""
    DEPOSIT = auto()
    WITHDRAWAL = auto()
    TRANSFER = auto()
    PAYMENT = auto()
    REFUND = auto()
    FEE = auto()
    INTEREST = auto()
    ADJUSTMENT = auto()


class AccountStatus(Enum):
    """Account lifecycle states."""
    PENDING = auto()
    ACTIVE = auto()
    SUSPENDED = auto()
    CLOSED = auto()
    FROZEN = auto()


@dataclass
class Transaction:
    """A single financial transaction record."""
    transaction_id: str
    account_id: str
    transaction_type: TransactionType
    amount: Decimal
    currency: str = "USD"
    description: str = ""
    reference: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    balance_after: Optional[Decimal] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            self.amount = Decimal(str(self.amount))
        if self.amount < 0 and self.transaction_type not in (
            TransactionType.WITHDRAWAL,
            TransactionType.FEE,
            TransactionType.ADJUSTMENT,
        ):
            raise ValueError(f"Negative amount not allowed for {self.transaction_type}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "account_id": self.account_id,
            "transaction_type": self.transaction_type.name,
            "amount": str(self.amount),
            "currency": self.currency,
            "description": self.description,
            "reference": self.reference,
            "timestamp": self.timestamp.isoformat(),
            "balance_after": str(self.balance_after) if self.balance_after else None,
            "metadata": self.metadata,
        }


@dataclass
class Account:
    """A user account with balance and transaction history."""
    account_id: str
    user_id: str
    status: AccountStatus = AccountStatus.PENDING
    balance: Decimal = Decimal("0")
    currency: str = "USD"
    created_at: datetime = field(default_factory=datetime.now)
    transactions: list[Transaction] = field(default_factory=list)
    overdraft_limit: Decimal = Decimal("0")
    interest_rate: Decimal = Decimal("0")
    minimum_balance: Decimal = Decimal("0")
    holder_name: str = ""
    holder_email: str = ""
    holder_phone: str = ""
    branch_code: str = ""
    tags: set[str] = field(default_factory=set)

    def deposit(self, amount: Union[Decimal, float, int], description: str = "") -> Transaction:
        """Deposit funds into the account."""
        amount = Decimal(str(amount))
        if amount <= 0:
            raise ValueError("Deposit amount must be positive")
        self.balance += amount
        txn = Transaction(
            transaction_id=f"TXN-{len(self.transactions):08d}",
            account_id=self.account_id,
            transaction_type=TransactionType.DEPOSIT,
            amount=amount,
            currency=self.currency,
            description=description or "Deposit",
            balance_after=self.balance,
        )
        self.transactions.append(txn)
        logger.info(f"Deposited {amount} to account {self.account_id}")
        return txn

    def withdraw(self, amount: Union[Decimal, float, int], description: str = "") -> Transaction:
        """Withdraw funds from the account."""
        amount = Decimal(str(amount))
        if amount <= 0:
            raise ValueError("Withdrawal amount must be positive")
        available = self.balance + self.overdraft_limit
        if amount > available:
            raise ValueError(f"Insufficient funds: requested {amount}, available {available}")
        self.balance -= amount
        txn = Transaction(
            transaction_id=f"TXN-{len(self.transactions):08d}",
            account_id=self.account_id,
            transaction_type=TransactionType.WITHDRAWAL,
            amount=amount,
            currency=self.currency,
            description=description or "Withdrawal",
            balance_after=self.balance,
        )
        self.transactions.append(txn)
        logger.info(f"Withdrew {amount} from account {self.account_id}")
        return txn

    def transfer(self, target: "Account", amount: Union[Decimal, float, int], description: str = "") -> tuple[Transaction, Transaction]:
        """Transfer funds to another account."""
        amount = Decimal(str(amount))
        if amount <= 0:
            raise ValueError("Transfer amount must be positive")
        withdrawal = self.withdraw(amount, f"Transfer to {target.account_id}: {description}")
        target.deposit(amount, f"Transfer from {self.account_id}: {description}")
        transfer_txn = Transaction(
            transaction_id=f"TXN-{len(self.transactions):08d}",
            account_id=self.account_id,
            transaction_type=TransactionType.TRANSFER,
            amount=amount,
            currency=self.currency,
            description=description,
            balance_after=self.balance,
        )
        self.transactions.append(transfer_txn)
        return withdrawal, transfer_txn

    def apply_interest(self) -> Optional[Transaction]:
        """Apply accrued interest to the account."""
        if self.interest_rate <= 0 or self.balance <= 0:
            return None
        interest = (self.balance * self.interest_rate / Decimal("365")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if interest <= 0:
            return None
        return self.deposit(interest, "Interest accrual")

    def get_statement(self, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None) -> list[Transaction]:
        """Get a statement of transactions within a date range."""
        if start_date is None:
            start_date = datetime.min
        if end_date is None:
            end_date = datetime.max
        return [
            txn for txn in self.transactions
            if start_date <= txn.timestamp <= end_date
        ]

    def close(self) -> None:
        """Close the account after settling balances."""
        if self.balance != 0:
            raise ValueError(f"Cannot close account with non-zero balance: {self.balance}")
        self.status = AccountStatus.CLOSED
        logger.info(f"Closed account {self.account_id}")


class AccountManager:
    """Manages a collection of accounts with persistence support."""

    def __init__(self, storage_path: Optional[Path] = None) -> None:
        self._accounts: dict[str, Account] = {}
        self._storage_path = storage_path
        self._lock = threading.Lock()
        if storage_path and storage_path.exists():
            self.load()

    def create_account(self, user_id: str, holder_name: str, holder_email: str, **kwargs: Any) -> Account:
        """Create a new account with a unique ID."""
        import uuid
        account_id = f"ACC-{uuid.uuid4().hex[:12].upper()}"
        account = Account(
            account_id=account_id,
            user_id=user_id,
            holder_name=holder_name,
            holder_email=holder_email,
            **kwargs,
        )
        with self._lock:
            self._accounts[account_id] = account
        logger.info(f"Created account {account_id} for user {user_id}")
        return account

    def get_account(self, account_id: str) -> Optional[Account]:
        """Retrieve an account by ID."""
        return self._accounts.get(account_id)

    def save(self) -> None:
        """Persist accounts to storage."""
        if not self._storage_path:
            return
        data = {
            aid: {
                "account_id": acc.account_id,
                "user_id": acc.user_id,
                "status": acc.status.name,
                "balance": str(acc.balance),
                "currency": acc.currency,
                "created_at": acc.created_at.isoformat(),
                "holder_name": acc.holder_name,
                "holder_email": acc.holder_email,
                "transactions": [txn.to_dict() for txn in acc.transactions],
            }
            for aid, acc in self._accounts.items()
        }
        self._storage_path.write_text(json.dumps(data, indent=2, default=str))

    def load(self) -> None:
        """Load accounts from storage."""
        if not self._storage_path or not self._storage_path.exists():
            return
        data = json.loads(self._storage_path.read_text())
        for aid, acc_data in data.items():
            account = Account(
                account_id=acc_data["account_id"],
                user_id=acc_data["user_id"],
                status=AccountStatus[acc_data["status"]],
                balance=Decimal(acc_data["balance"]),
                currency=acc_data["currency"],
                created_at=datetime.fromisoformat(acc_data["created_at"]),
                holder_name=acc_data.get("holder_name", ""),
                holder_email=acc_data.get("holder_email", ""),
            )
            self._accounts[aid] = account
'''


def _make_real_python_file(path: Path) -> Path:
    """Write a realistic Python file that passes the generated_like check."""
    path.write_text(_REAL_PYTHON, encoding="utf-8")
    return path


def _run_read_cache(snapshot_dir: Path, payload: dict, extra_env: dict | None = None):
    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(snapshot_dir)
    env["TOKEN_OPTIMIZER_READ_CACHE"] = "1"
    env.setdefault("TOKEN_OPTIMIZER_FIRST_READ_ACTIVE", "1")
    env.setdefault("TOKEN_OPTIMIZER_FIRST_READ_SHADOW", "1")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(READ_CACHE)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _parse_stdout(out: str) -> dict | None:
    out = out.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _make_payload(file_path: Path, tool_use_id: str = "toolu_01") -> dict:
    # Use the resolved path so it matches what read_cache.py stores internally
    resolved = str(Path(file_path).resolve())
    return {
        "tool_name": "Read",
        "tool_input": {"file_path": resolved, "offset": 0, "limit": 0},
        "tool_use_id": tool_use_id,
        "session_id": SESSION,
        "agent_id": SESSION,
    }


def test_double_fire_denies_first_read_as_redundant(tmp_path):
    """THE DAMAGE: a first-ever Read is denied as redundant when the hook
    fires twice for the same tool call.

    Fire #1 creates the cache entry (read_count=1). Fire #2 sees the entry,
    mtime/size match, range is covered, so it denies the read as "redundant".

    Before F1b: the second fire DENIES the read (the damage).
    After F1b: the second fire sees the same tool_use_id and ALLOWS the read
    (it is the same tool call, not a genuine reread).
    """
    f = _make_real_python_file(tmp_path / "target.py")
    payload = _make_payload(f, tool_use_id="toolu_double_fire_01")

    # Fire #1: first read. Must allow and must create the cache entry.
    out1 = _run_read_cache(tmp_path, payload)
    assert out1.returncode == 0, out1.stderr
    parsed1 = _parse_stdout(out1.stdout)
    # First fire should NOT deny (it's a first read)
    if parsed1 is not None:
        decision = parsed1.get("hookSpecificOutput", {}).get("permissionDecision")
        assert decision != "deny", (
            f"First fire should not deny a first-ever read, got: {parsed1}"
        )

    # Fire #2: SAME tool call (same tool_use_id). This is the damage scenario:
    # the double-hook registration makes the same PreToolUse/Read fire twice.
    # Without the guard, the second fire sees the cache entry from fire #1 and denies
    # the read as "redundant" -- even though the user has never actually read
    # this file before (the first fire's deny/skeleton prevented the full file
    # from entering context, or the first fire allowed it but the second fire
    # blocks the same tool call).
    out2 = _run_read_cache(tmp_path, payload)
    assert out2.returncode == 0, out2.stderr
    parsed2 = _parse_stdout(out2.stdout)
    # After F1b: the second fire of the SAME tool_use_id must NOT deny.
    # An allow is emitted as no output (the default) or as a non-deny decision.
    if parsed2 is not None:
        decision2 = parsed2.get("hookSpecificOutput", {}).get("permissionDecision")
        assert decision2 != "deny", (
            f"Second fire of the same tool_use_id denied the read as redundant. "
            f"This is the double-fire damage: a first-ever read blocked because "
            f"the hook fired twice. Decision: {decision2}, payload: {parsed2}"
        )
    # If parsed2 is None (empty stdout), that means the read was allowed
    # silently, which is the correct F1b behavior.


def test_genuine_reread_is_still_blocked(tmp_path):
    """REGRESSION GUARD: after F1b, a genuine reread (DIFFERENT tool_use_id)
    of an unchanged file must STILL be blocked as redundant. The tool_use_id
    guard must only suppress denials for the SAME tool call, not for genuine
    rereads.
    """
    f = _make_real_python_file(tmp_path / "target.py")

    # First read (tool_use_id=A)
    payload_a = _make_payload(f, tool_use_id="toolu_reread_A")
    out1 = _run_read_cache(tmp_path, payload_a)
    assert out1.returncode == 0, out1.stderr

    # Second read of the same file (DIFFERENT tool_use_id=B) -- genuine reread
    payload_b = _make_payload(f, tool_use_id="toolu_reread_B")
    out2 = _run_read_cache(tmp_path, payload_b)
    assert out2.returncode == 0, out2.stderr
    parsed2 = _parse_stdout(out2.stdout)
    assert parsed2 is not None, (
        f"Genuine reread produced no output. stdout={out2.stdout!r}, stderr={out2.stderr!r}"
    )
    decision2 = parsed2.get("hookSpecificOutput", {}).get("permissionDecision")
    # A genuine reread (different tool_use_id) of an unchanged file
    # SHOULD be denied as redundant (this is the read cache's job).
    assert decision2 == "deny", (
        f"Genuine reread (different tool_use_id) should be denied as "
        f"redundant, got decision: {decision2}, payload: {parsed2}"
    )
