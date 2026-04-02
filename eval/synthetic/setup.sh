#!/usr/bin/env bash
# Setup script for the split-pr synthetic evaluation dataset.
# This script initializes the git repo and creates the mega-pr branch
# with entangled commits that mix multiple change topics.
#
# Usage: bash /tmp/split-pr-eval/setup_synthetic.sh
#
# Prerequisites: All base project files must already exist in
#   /tmp/split-pr-eval/synthetic/

set -euo pipefail

REPO="${SYNTHETIC_DIR:-/tmp/split-pr-eval/synthetic}"

cd "$REPO"

###############################################################################
# Step 1: Initialize repo and create base commit on main
###############################################################################
git init
git checkout -b main
git add -A
git commit -m "Initial project structure: inventory management service

- models/: Product, Warehouse, Inventory domain models
- api/: FastAPI endpoints for products and inventory
- database/: Connection management and query repositories
- auth/: JWT tokens and role-based permissions
- utils/: Formatting and validation helpers
- tests/: Unit tests for all modules
- pyproject.toml: Project configuration"

echo "=== Base commit on main created ==="

###############################################################################
# Step 2: Create mega-pr branch
###############################################################################
git checkout -b mega-pr

###############################################################################
# COMMIT 1: Entangled — shared cache (topic c) + bug fix in database (topic d, part 1)
# This commit mixes infrastructure with a bug fix.
###############################################################################

# Topic C: shared cache infrastructure
cat > utils/cache.py << 'PYEOF'
"""Simple in-memory cache with TTL support.

Provides a lightweight caching layer for expensive database queries
and external API calls. Not suitable for multi-process deployments;
use Redis in production for shared state.
"""

from __future__ import annotations

import time
import threading
from typing import Any, Callable, Optional
from functools import wraps


class CacheEntry:
    """A single cached value with expiration metadata."""

    __slots__ = ("value", "expires_at", "created_at")

    def __init__(self, value: Any, ttl_seconds: float):
        self.value = value
        self.created_at = time.monotonic()
        self.expires_at = self.created_at + ttl_seconds

    @property
    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.created_at


class TTLCache:
    """Thread-safe in-memory cache with per-key TTL.

    Usage:
        cache = TTLCache(default_ttl=300)
        cache.set("key", expensive_result)
        value = cache.get("key")  # returns None if expired

    For function-level caching, use the decorator:
        @cache.cached(ttl=60)
        def get_product(product_id):
            ...
    """

    def __init__(self, default_ttl: float = 300.0, max_size: int = 10000):
        self._store: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self.default_ttl = default_ttl
        self.max_size = max_size
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        """Get a value from cache. Returns None if missing or expired."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            if entry.is_expired:
                del self._store[key]
                self._misses += 1
                return None
            self._hits += 1
            return entry.value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Store a value in cache with optional custom TTL."""
        with self._lock:
            if len(self._store) >= self.max_size:
                self._evict_expired()
            self._store[key] = CacheEntry(value, ttl or self.default_ttl)

    def delete(self, key: str) -> bool:
        """Remove a key from cache. Returns True if the key existed."""
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def clear(self) -> int:
        """Clear all entries. Returns the number of entries removed."""
        with self._lock:
            count = len(self._store)
            self._store.clear()
            return count

    def _evict_expired(self) -> None:
        """Remove all expired entries. Must be called with lock held."""
        expired_keys = [k for k, v in self._store.items() if v.is_expired]
        for key in expired_keys:
            del self._store[key]

    @property
    def stats(self) -> dict[str, int]:
        """Return cache hit/miss statistics."""
        return {
            "hits": self._hits,
            "misses": self._misses,
            "size": len(self._store),
            "hit_rate_pct": round(
                self._hits / max(self._hits + self._misses, 1) * 100, 1
            ),
        }

    def cached(self, ttl: Optional[float] = None, key_prefix: str = ""):
        """Decorator to cache function results.

        Args:
            ttl: Cache TTL in seconds. Uses default_ttl if not specified.
            key_prefix: Prefix for cache keys to namespace entries.
        """
        def decorator(func: Callable) -> Callable:
            @wraps(func)
            def wrapper(*args, **kwargs):
                cache_key = f"{key_prefix}{func.__name__}:{args}:{sorted(kwargs.items())}"
                result = self.get(cache_key)
                if result is not None:
                    return result
                result = func(*args, **kwargs)
                self.set(cache_key, result, ttl)
                return result
            return wrapper
        return decorator


# Module-level singleton for convenience
_default_cache: Optional[TTLCache] = None


def get_cache(ttl: float = 300.0) -> TTLCache:
    """Get or create the default cache singleton."""
    global _default_cache
    if _default_cache is None:
        _default_cache = TTLCache(default_ttl=ttl)
    return _default_cache
PYEOF

# Topic D (part 1): Bug fix — get_low_stock_items has wrong comparison operator
# Fix: the query should use <= not < for the threshold
sed -i 's/WHERE i.quantity < :threshold AND p.is_active = true/WHERE i.quantity <= :threshold AND p.is_active = true/' database/queries.py

git add utils/cache.py database/queries.py
git commit -m "Add caching utility and fix low-stock query threshold

- New TTLCache with thread-safe operations and decorator support
- Fix off-by-one: low stock query should use <= not < for threshold"

echo "=== Commit 1 done (cache + bug fix part 1) ==="

###############################################################################
# COMMIT 2: Entangled — clean feature (topic a, part 1) + test infra (topic g, part 1)
# Mixes a new feature model with test infrastructure.
###############################################################################

# Topic A (part 1): New preferences model
cat > models/preferences.py << 'PYEOF'
"""User preferences for notification and display settings."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


class NotificationChannel(enum.Enum):
    """Supported notification delivery channels."""

    EMAIL = "email"
    SLACK = "slack"
    WEBHOOK = "webhook"
    SMS = "sms"


class AlertThreshold(enum.Enum):
    """When to trigger stock alerts."""

    IMMEDIATE = "immediate"
    DAILY_DIGEST = "daily_digest"
    WEEKLY_DIGEST = "weekly_digest"
    NEVER = "never"


@dataclass
class NotificationPreference:
    """A single notification channel configuration."""

    channel: NotificationChannel
    enabled: bool = True
    destination: str = ""  # email address, webhook URL, etc.
    min_severity: str = "low"

    def validate(self) -> list[str]:
        """Validate the notification preference. Returns list of errors."""
        errors = []
        if self.enabled and not self.destination:
            errors.append(f"Destination required for enabled {self.channel.value} channel")
        if self.channel == NotificationChannel.EMAIL and self.destination:
            if "@" not in self.destination:
                errors.append(f"Invalid email: {self.destination}")
        if self.channel == NotificationChannel.WEBHOOK and self.destination:
            if not self.destination.startswith(("http://", "https://")):
                errors.append(f"Webhook URL must start with http(s)://")
        return errors


@dataclass
class UserPreferences:
    """Complete set of user preferences for the inventory system."""

    user_id: UUID
    id: UUID = field(default_factory=uuid4)
    display_name: Optional[str] = None
    timezone: str = "UTC"
    locale: str = "en-US"
    items_per_page: int = 50
    alert_threshold: AlertThreshold = AlertThreshold.DAILY_DIGEST
    notifications: list[NotificationPreference] = field(default_factory=list)
    favorite_warehouses: list[UUID] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None

    def __post_init__(self):
        if self.items_per_page < 10:
            self.items_per_page = 10
        elif self.items_per_page > 200:
            self.items_per_page = 200

    def add_notification_channel(
        self, channel: NotificationChannel, destination: str
    ) -> None:
        """Add a new notification channel."""
        pref = NotificationPreference(
            channel=channel, destination=destination
        )
        errors = pref.validate()
        if errors:
            raise ValueError(f"Invalid notification config: {'; '.join(errors)}")
        # Replace existing channel config if present
        self.notifications = [
            n for n in self.notifications if n.channel != channel
        ]
        self.notifications.append(pref)
        self.updated_at = datetime.utcnow()

    def remove_notification_channel(self, channel: NotificationChannel) -> bool:
        """Remove a notification channel. Returns True if it was present."""
        before = len(self.notifications)
        self.notifications = [
            n for n in self.notifications if n.channel != channel
        ]
        if len(self.notifications) < before:
            self.updated_at = datetime.utcnow()
            return True
        return False

    def get_active_channels(self) -> list[NotificationChannel]:
        """Get list of enabled notification channels."""
        return [n.channel for n in self.notifications if n.enabled]

    def toggle_favorite_warehouse(self, warehouse_id: UUID) -> bool:
        """Toggle a warehouse as favorite. Returns True if added, False if removed."""
        if warehouse_id in self.favorite_warehouses:
            self.favorite_warehouses.remove(warehouse_id)
            self.updated_at = datetime.utcnow()
            return False
        else:
            self.favorite_warehouses.append(warehouse_id)
            self.updated_at = datetime.utcnow()
            return True

    def validate(self) -> list[str]:
        """Validate all preferences. Returns list of error messages."""
        errors = []
        for notif in self.notifications:
            errors.extend(notif.validate())
        if self.timezone and "/" not in self.timezone and self.timezone != "UTC":
            errors.append(f"Suspicious timezone format: {self.timezone}")
        return errors
PYEOF

# Topic G (part 1): Test infrastructure — conftest with fixtures
cat > tests/conftest.py << 'PYEOF'
"""Shared test fixtures and configuration."""

import os
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest

from models.product import Product, ProductCategory, ProductDimensions
from models.warehouse import Warehouse, StorageZone, ZoneType
from models.inventory import InventoryItem


# ---------------------------------------------------------------------------
# Product fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_dimensions():
    """Standard product dimensions for testing."""
    return ProductDimensions(length=30, width=20, height=15, weight=1.5)


@pytest.fixture
def sample_product(sample_dimensions):
    """A standard test product."""
    return Product(
        sku="TEST-WIDGET-001",
        name="Test Widget",
        category=ProductCategory.GENERAL,
        dimensions=sample_dimensions,
        barcode="1234567890123",
        min_stock_level=10,
        max_stock_level=500,
    )


@pytest.fixture
def fragile_product(sample_dimensions):
    """A fragile product requiring special handling."""
    return Product(
        sku="FRAG-VASE-001",
        name="Crystal Vase",
        category=ProductCategory.FRAGILE,
        dimensions=sample_dimensions,
    )


@pytest.fixture
def perishable_product():
    """A perishable product with cold storage requirements."""
    dims = ProductDimensions(length=10, width=10, height=10, weight=0.5)
    return Product(
        sku="PRSH-MILK-001",
        name="Organic Milk",
        category=ProductCategory.PERISHABLE,
        dimensions=dims,
    )


# ---------------------------------------------------------------------------
# Warehouse fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_warehouse():
    """A fully configured test warehouse."""
    wh = Warehouse(
        name="Test Distribution Center",
        location="Copenhagen",
        manager_email="mgr@example.com",
    )
    wh.zones = [
        StorageZone(
            name="General Storage A",
            zone_type=ZoneType.STANDARD,
            capacity=1000,
            warehouse_id=wh.id,
        ),
        StorageZone(
            name="General Storage B",
            zone_type=ZoneType.STANDARD,
            capacity=800,
            warehouse_id=wh.id,
        ),
        StorageZone(
            name="Cold Room",
            zone_type=ZoneType.COLD_STORAGE,
            capacity=200,
            warehouse_id=wh.id,
        ),
        StorageZone(
            name="Hazmat Cage",
            zone_type=ZoneType.HAZMAT,
            capacity=50,
            warehouse_id=wh.id,
        ),
        StorageZone(
            name="High Value Vault",
            zone_type=ZoneType.HIGH_VALUE,
            capacity=100,
            warehouse_id=wh.id,
        ),
    ]
    return wh


@pytest.fixture
def empty_warehouse():
    """An empty warehouse with no zones."""
    return Warehouse(name="Empty Facility", location="Aarhus")


# ---------------------------------------------------------------------------
# Inventory fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stocked_item(sample_product, sample_warehouse):
    """An inventory item with some stock."""
    zone = sample_warehouse.zones[0]  # General Storage A
    return InventoryItem(
        product_id=sample_product.id,
        zone_id=zone.id,
        quantity=100,
        lot_number="LOT-2024-001",
    )


@pytest.fixture
def expired_item(sample_product, sample_warehouse):
    """An inventory item that has expired."""
    zone = sample_warehouse.zones[2]  # Cold Room
    return InventoryItem(
        product_id=sample_product.id,
        zone_id=zone.id,
        quantity=50,
        lot_number="LOT-2024-EXP",
        expiry_date=datetime.utcnow() - timedelta(days=30),
    )


# ---------------------------------------------------------------------------
# Auth fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_token():
    """A valid admin JWT token."""
    from auth.tokens import create_access_token
    return create_access_token(
        subject="admin@inventory.test",
        roles=["admin"],
    )


@pytest.fixture
def operator_token():
    """A valid operator JWT token."""
    from auth.tokens import create_access_token
    return create_access_token(
        subject="operator@inventory.test",
        roles=["operator"],
    )


@pytest.fixture
def viewer_token():
    """A valid viewer JWT token."""
    from auth.tokens import create_access_token
    return create_access_token(
        subject="viewer@inventory.test",
        roles=["viewer"],
    )
PYEOF

git add models/preferences.py tests/conftest.py
git commit -m "Add user preferences model and shared test fixtures

- UserPreferences with notification channels and alert settings
- conftest.py with reusable fixtures for products, warehouses, auth"

echo "=== Commit 2 done (preferences model + test fixtures) ==="

###############################################################################
# COMMIT 3: Entangled — clean feature (topic a, part 2) + cross-cutting refactor (topic b)
# Mixes the preferences API endpoint with a rename of build_log_prefix -> log_context
###############################################################################

# Topic B: Rename build_log_prefix -> log_context across the codebase
# This is a cross-cutting rename that touches many files.
sed -i 's/def build_log_prefix/def log_context/' utils/formatting.py
sed -i 's/build_log_prefix/log_context/' utils/formatting.py
sed -i 's/from utils.formatting import format_sku, format_quantity, slugify/from utils.formatting import format_sku, format_quantity, slugify, log_context/' utils/__init__.py
sed -i 's/"slugify",/"slugify",\n    "log_context",/' utils/__init__.py
sed -i '/build_log_prefix/d' utils/__init__.py
sed -i 's/build_log_prefix/log_context/g' database/queries.py
sed -i 's/build_log_prefix/log_context/g' api/products.py
sed -i 's/build_log_prefix/log_context/g' api/inventory.py

# Topic A (part 2): Preferences API endpoint
cat > api/preferences.py << 'PYEOF'
"""User preferences API endpoints."""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from utils.cache import get_cache
from utils.formatting import log_context

router = APIRouter(prefix="/preferences", tags=["preferences"])

# Cache preferences for 5 minutes
_cache = get_cache()


class NotificationConfig(BaseModel):
    """A notification channel configuration."""

    channel: str = Field(..., description="One of: email, slack, webhook, sms")
    enabled: bool = True
    destination: str = Field(..., min_length=1)


class PreferencesUpdate(BaseModel):
    """Request body for updating user preferences."""

    display_name: Optional[str] = None
    timezone: str = Field(default="UTC")
    locale: str = Field(default="en-US")
    items_per_page: int = Field(default=50, ge=10, le=200)
    alert_threshold: str = Field(default="daily_digest")
    notifications: list[NotificationConfig] = Field(default_factory=list)


class PreferencesResponse(BaseModel):
    """Response body with user preferences."""

    user_id: str
    display_name: Optional[str]
    timezone: str
    locale: str
    items_per_page: int
    alert_threshold: str
    notifications: list[NotificationConfig]
    favorite_warehouses: list[str]


@router.get("/{user_id}", response_model=PreferencesResponse)
async def get_preferences(user_id: UUID):
    """Get preferences for a user."""
    prefix = log_context("api", "get_preferences", str(user_id))

    # Check cache first
    cache_key = f"prefs:{user_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    # In a real app this would query the database
    return PreferencesResponse(
        user_id=str(user_id),
        display_name=None,
        timezone="UTC",
        locale="en-US",
        items_per_page=50,
        alert_threshold="daily_digest",
        notifications=[],
        favorite_warehouses=[],
    )


@router.put("/{user_id}", response_model=PreferencesResponse)
async def update_preferences(user_id: UUID, body: PreferencesUpdate):
    """Update preferences for a user."""
    prefix = log_context("api", "update_preferences", str(user_id))

    # Invalidate cache
    _cache.delete(f"prefs:{user_id}")

    return PreferencesResponse(
        user_id=str(user_id),
        display_name=body.display_name,
        timezone=body.timezone,
        locale=body.locale,
        items_per_page=body.items_per_page,
        alert_threshold=body.alert_threshold,
        notifications=body.notifications,
        favorite_warehouses=[],
    )


@router.post("/{user_id}/favorites/{warehouse_id}")
async def toggle_favorite(user_id: UUID, warehouse_id: UUID):
    """Toggle a warehouse as a favorite."""
    prefix = log_context("api", "toggle_favorite", str(user_id))
    _cache.delete(f"prefs:{user_id}")
    return {"user_id": str(user_id), "warehouse_id": str(warehouse_id), "action": "toggled"}
PYEOF

git add utils/formatting.py utils/__init__.py database/queries.py \
       api/products.py api/inventory.py api/preferences.py
git commit -m "Add preferences endpoint and rename log helper for clarity

- New /preferences CRUD API with cache integration
- Rename build_log_prefix -> log_context across all modules for consistency"

echo "=== Commit 3 done (preferences API + rename refactor) ==="

###############################################################################
# COMMIT 4: Entangled — vendored code (topic e) + config update (topic h, part 1)
###############################################################################

# Topic E: Vendor a rate limiter library
mkdir -p _vendor/ratelimit

cat > _vendor/__init__.py << 'PYEOF'
PYEOF

cat > _vendor/ratelimit/__init__.py << 'PYEOF'
"""Vendored rate limiting library.

Adapted from github.com/example/ratelimit v2.1.0
License: MIT
"""

from _vendor.ratelimit.core import RateLimiter, RateLimitExceeded
from _vendor.ratelimit.storage import MemoryStorage
from _vendor.ratelimit.middleware import RateLimitMiddleware

__all__ = ["RateLimiter", "RateLimitExceeded", "MemoryStorage", "RateLimitMiddleware"]
PYEOF

cat > _vendor/ratelimit/core.py << 'PYEOF'
"""Core rate limiting logic."""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from typing import Optional, Protocol


class StorageBackend(Protocol):
    """Interface for rate limit state storage."""

    def get_window(self, key: str) -> Optional[tuple[float, int]]:
        """Get (window_start, count) for a key."""
        ...

    def increment(self, key: str, window_start: float, ttl: float) -> int:
        """Increment counter for key in window. Returns new count."""
        ...

    def reset(self, key: str) -> None:
        """Reset counter for key."""
        ...


class RateLimitExceeded(Exception):
    """Raised when a rate limit is exceeded."""

    def __init__(self, limit: int, window_seconds: float, retry_after: float):
        self.limit = limit
        self.window_seconds = window_seconds
        self.retry_after = retry_after
        super().__init__(
            f"Rate limit exceeded: {limit} requests per {window_seconds}s. "
            f"Retry after {retry_after:.1f}s"
        )


@dataclass
class RateLimitRule:
    """A rate limiting rule definition."""

    requests: int
    window_seconds: float
    key_prefix: str = ""

    def make_key(self, identifier: str) -> str:
        prefix = self.key_prefix or "rl"
        return f"{prefix}:{identifier}"


class RateLimiter:
    """Token bucket rate limiter with configurable storage.

    Usage:
        limiter = RateLimiter(storage=MemoryStorage())
        rule = RateLimitRule(requests=100, window_seconds=60)

        try:
            limiter.check(rule, identifier="user:123")
        except RateLimitExceeded as e:
            print(f"Retry after {e.retry_after}s")
    """

    def __init__(self, storage: StorageBackend):
        self._storage = storage

    def check(self, rule: RateLimitRule, identifier: str) -> int:
        """Check rate limit for an identifier.

        Args:
            rule: The rate limit rule to apply.
            identifier: Unique identifier (e.g., user ID, IP address).

        Returns:
            Number of remaining requests in the current window.

        Raises:
            RateLimitExceeded: If the limit has been exceeded.
        """
        key = rule.make_key(identifier)
        now = time.monotonic()
        window_start = now - (now % rule.window_seconds)

        count = self._storage.increment(key, window_start, rule.window_seconds)

        if count > rule.requests:
            retry_after = (window_start + rule.window_seconds) - now
            raise RateLimitExceeded(
                limit=rule.requests,
                window_seconds=rule.window_seconds,
                retry_after=max(0, retry_after),
            )

        return rule.requests - count

    def reset(self, rule: RateLimitRule, identifier: str) -> None:
        """Reset the counter for an identifier."""
        key = rule.make_key(identifier)
        self._storage.reset(key)

    def get_remaining(self, rule: RateLimitRule, identifier: str) -> int:
        """Get remaining requests without incrementing."""
        key = rule.make_key(identifier)
        window = self._storage.get_window(key)
        if window is None:
            return rule.requests
        _, count = window
        return max(0, rule.requests - count)
PYEOF

cat > _vendor/ratelimit/storage.py << 'PYEOF'
"""Storage backends for rate limit state."""

from __future__ import annotations

import time
import threading
from typing import Optional


class MemoryStorage:
    """In-memory storage backend for rate limiting.

    Thread-safe but not shared across processes.
    For production, use RedisStorage instead.
    """

    def __init__(self):
        self._data: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def get_window(self, key: str) -> Optional[tuple[float, int]]:
        """Get current window data for a key."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            window_start, count = entry
            return (window_start, count)

    def increment(self, key: str, window_start: float, ttl: float) -> int:
        """Increment counter, resetting if window has changed."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None or entry[0] != window_start:
                # New window
                self._data[key] = (window_start, 1)
                return 1
            else:
                new_count = entry[1] + 1
                self._data[key] = (window_start, new_count)
                return new_count

    def reset(self, key: str) -> None:
        """Remove rate limit state for a key."""
        with self._lock:
            self._data.pop(key, None)

    def cleanup_expired(self, max_age: float = 3600) -> int:
        """Remove entries older than max_age seconds. Returns count removed."""
        now = time.monotonic()
        with self._lock:
            expired = [
                k for k, (ws, _) in self._data.items()
                if now - ws > max_age
            ]
            for key in expired:
                del self._data[key]
            return len(expired)
PYEOF

cat > _vendor/ratelimit/middleware.py << 'PYEOF'
"""FastAPI middleware for rate limiting."""

from __future__ import annotations

from typing import Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from _vendor.ratelimit.core import RateLimiter, RateLimitRule, RateLimitExceeded
from _vendor.ratelimit.storage import MemoryStorage


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply rate limiting to all API requests.

    The client is identified by the X-API-Key header or remote IP.

    Usage:
        app = FastAPI()
        app.add_middleware(
            RateLimitMiddleware,
            requests_per_minute=60,
        )
    """

    def __init__(
        self,
        app,
        requests_per_minute: int = 60,
        storage: Optional[MemoryStorage] = None,
        key_func: Optional[Callable[[Request], str]] = None,
    ):
        super().__init__(app)
        self._storage = storage or MemoryStorage()
        self._limiter = RateLimiter(self._storage)
        self._rule = RateLimitRule(
            requests=requests_per_minute,
            window_seconds=60.0,
            key_prefix="api",
        )
        self._key_func = key_func or self._default_key

    @staticmethod
    def _default_key(request: Request) -> str:
        """Extract client identifier from request."""
        api_key = request.headers.get("X-API-Key")
        if api_key:
            return f"key:{api_key}"
        client = request.client
        if client:
            return f"ip:{client.host}"
        return "unknown"

    async def dispatch(self, request: Request, call_next):
        identifier = self._key_func(request)

        try:
            remaining = self._limiter.check(self._rule, identifier)
        except RateLimitExceeded as exc:
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Rate limit exceeded",
                    "retry_after": round(exc.retry_after, 1),
                },
                headers={
                    "Retry-After": str(int(exc.retry_after) + 1),
                    "X-RateLimit-Limit": str(exc.limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self._rule.requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
PYEOF

# Topic H (part 1): Add redis dependency to pyproject.toml
cat > pyproject.toml << 'PYEOF'
[build-system]
requires = ["setuptools>=68.0", "wheel"]
build-backend = "setuptools.backends._legacy:_Backend"

[project]
name = "inventory-service"
version = "1.3.0"
description = "Inventory management service for warehouse operations"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.104.0",
    "uvicorn>=0.24.0",
    "sqlalchemy>=2.0.23",
    "pydantic>=2.5.0",
    "python-jose>=3.3.0",
    "passlib>=1.7.4",
    "httpx>=0.25.0",
    "redis>=5.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.4.0",
    "pytest-asyncio>=0.23.0",
    "coverage>=7.3.0",
    "pytest-cov>=4.1.0",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.coverage.run]
source = ["models", "api", "database", "auth", "utils"]
omit = ["tests/*", "_vendor/*"]
PYEOF

git add _vendor/ pyproject.toml
git commit -m "Vendor rate limiting library and update project config

- Add vendored ratelimit package from github.com/example/ratelimit v2.1.0
- Bump version to 1.3.0, add redis dependency
- Add pytest-cov and coverage config"

echo "=== Commit 4 done (vendored code + config update part 1) ==="

###############################################################################
# COMMIT 5: Entangled — bug fix part 2 (topic d) + test-only (topic g, part 2)
#            + empty __init__.py (topic f)
# This commit mixes three topics: a new query method (not a bug fix),
# new integration test setup, and a new package with empty init files.
###############################################################################

# Topic D (part 2): New query method in database (separate from bug fix)
cat >> database/queries.py << 'PYEOF'


class WarehouseRepository:
    """Data access layer for warehouse operations."""

    def __init__(self, session: Session):
        self.session = session

    def get_by_id(self, warehouse_id: UUID) -> Optional[dict]:
        """Fetch a warehouse by its primary key."""
        result = self.session.execute(
            text("SELECT * FROM warehouses WHERE id = :id"),
            {"id": str(warehouse_id)},
        )
        row = result.fetchone()
        return dict(row._mapping) if row else None

    def list_operational(self) -> list[dict]:
        """List all operational warehouses."""
        result = self.session.execute(
            text(
                "SELECT * FROM warehouses WHERE is_operational = true "
                "ORDER BY name"
            )
        )
        return [dict(row._mapping) for row in result]

    def get_zone_utilization(self, warehouse_id: UUID) -> list[dict]:
        """Get utilization stats for all zones in a warehouse."""
        prefix = log_context("warehouse", "zone_utilization", str(warehouse_id))
        result = self.session.execute(
            text(
                "SELECT z.name, z.zone_type, z.capacity, z.current_occupancy, "
                "ROUND(z.current_occupancy::numeric / NULLIF(z.capacity, 0) * 100, 1) as utilization_pct "
                "FROM storage_zones z "
                "WHERE z.warehouse_id = :wid "
                "ORDER BY z.name"
            ),
            {"wid": str(warehouse_id)},
        )
        return [dict(row._mapping) for row in result]

    def find_available_zones(
        self, warehouse_id: UUID, zone_type: str, min_capacity: int = 1
    ) -> list[dict]:
        """Find zones with available capacity."""
        result = self.session.execute(
            text(
                "SELECT * FROM storage_zones "
                "WHERE warehouse_id = :wid AND zone_type = :ztype "
                "AND (capacity - current_occupancy) >= :min_cap "
                "AND is_active = true "
                "ORDER BY (capacity - current_occupancy) DESC"
            ),
            {
                "wid": str(warehouse_id),
                "ztype": zone_type,
                "min_cap": min_capacity,
            },
        )
        return [dict(row._mapping) for row in result]
PYEOF

# Need to add missing import for log_context at the top of queries.py
sed -i 's/from utils.formatting import log_context/from utils.formatting import log_context/' database/queries.py

# Topic F: New reporting package with empty __init__.py files
mkdir -p reporting/exporters

touch reporting/__init__.py
touch reporting/exporters/__init__.py

cat > reporting/generators.py << 'PYEOF'
"""Report generation logic.

This module will eventually contain report generators for inventory,
warehouse utilization, and movement history reports.
"""

from __future__ import annotations

from typing import Any


class ReportGenerator:
    """Base class for generating reports."""

    def __init__(self, title: str):
        self.title = title
        self._data: list[dict[str, Any]] = []

    def add_row(self, row: dict[str, Any]) -> None:
        """Add a data row to the report."""
        self._data.append(row)

    def row_count(self) -> int:
        return len(self._data)

    def generate(self) -> dict[str, Any]:
        """Generate the report as a dictionary."""
        return {
            "title": self.title,
            "row_count": self.row_count(),
            "data": self._data,
        }
PYEOF

# Topic G (part 2): Integration test scaffolding
cat > tests/test_integration.py << 'PYEOF'
"""Integration test scaffolding.

These tests verify cross-module interactions. They use the shared fixtures
from conftest.py to set up realistic test scenarios.
"""

from uuid import uuid4

import pytest

from models.product import Product, ProductCategory, ProductDimensions
from models.warehouse import Warehouse, StorageZone, ZoneType
from models.inventory import InventoryItem, StockLevel


class TestWarehouseProductIntegration:
    """Test interactions between warehouse zones and products."""

    def test_product_placement_in_correct_zone(
        self, sample_warehouse, sample_product
    ):
        """Products should be placed in appropriate zone types."""
        zone = sample_warehouse.find_zone_for_product(sample_product.category)
        assert zone is not None
        assert zone.zone_type == ZoneType.STANDARD

    def test_fragile_product_goes_to_high_value(
        self, sample_warehouse, fragile_product
    ):
        """Fragile products should be routed to high-value zones."""
        zone = sample_warehouse.find_zone_for_product(fragile_product.category)
        assert zone is not None
        assert zone.zone_type == ZoneType.HIGH_VALUE

    def test_perishable_product_goes_to_cold(
        self, sample_warehouse, perishable_product
    ):
        """Perishable products should be routed to cold storage."""
        zone = sample_warehouse.find_zone_for_product(perishable_product.category)
        assert zone is not None
        assert zone.zone_type == ZoneType.COLD_STORAGE

    def test_stock_level_classification_with_product_thresholds(
        self, sample_product, sample_warehouse
    ):
        """Stock levels should be classified using product thresholds."""
        zone = sample_warehouse.zones[0]
        item = InventoryItem(
            product_id=sample_product.id,
            zone_id=zone.id,
            quantity=5,  # below min_stock_level of 10
        )
        level = item.classify_stock_level(
            sample_product.min_stock_level,
            sample_product.max_stock_level,
        )
        assert level == StockLevel.LOW

    def test_zone_allocation_updates_capacity(self, sample_warehouse):
        """Allocating items to a zone should reduce available capacity."""
        zone = sample_warehouse.zones[0]
        initial = zone.available_capacity
        zone.allocate(50)
        assert zone.available_capacity == initial - 50

    def test_warehouse_shutdown_deactivates_zones(self, sample_warehouse):
        """Shutting down a warehouse should deactivate all zones."""
        sample_warehouse.shutdown()
        assert sample_warehouse.is_operational is False
        for zone in sample_warehouse.zones:
            assert zone.is_active is False


class TestInventoryMovementFlow:
    """Test realistic inventory movement scenarios."""

    def test_inbound_then_transfer(self, sample_product, sample_warehouse):
        """Simulate receiving stock and transferring between zones."""
        source_zone = sample_warehouse.zones[0]
        dest_zone = sample_warehouse.zones[1]

        # Receive inbound stock
        item = InventoryItem(
            product_id=sample_product.id,
            zone_id=source_zone.id,
            quantity=100,
        )
        source_zone.allocate(100)

        # Transfer partial stock
        item.adjust_quantity(-30)
        source_zone.release(30)
        dest_zone.allocate(30)

        assert item.quantity == 70
        assert source_zone.current_occupancy == 100 - 30

    def test_expired_item_detection(self, expired_item):
        """Expired items should be detected correctly."""
        assert expired_item.is_expired() is True

    def test_physical_count_reconciliation(self, stocked_item):
        """Physical count should update quantity and record discrepancy."""
        discrepancy = stocked_item.record_count(95)
        assert discrepancy == -5
        assert stocked_item.quantity == 95
        assert stocked_item.last_counted_at is not None
PYEOF

git add database/queries.py reporting/ tests/test_integration.py
git commit -m "Add warehouse queries, reporting module, and integration tests

- WarehouseRepository with zone utilization and availability queries
- New reporting package structure (generators, exporters)
- Integration tests for warehouse-product interactions"

echo "=== Commit 5 done (new query + reporting + integration tests) ==="

###############################################################################
# COMMIT 6: Clean — test for preferences (topic a, part 3) + config finalization (topic h, part 2)
# This commit mostly belongs to topic A but also touches config.
###############################################################################

# Topic A (part 3): Test for the preferences feature
cat > tests/test_preferences.py << 'PYEOF'
"""Tests for user preferences model and API."""

from uuid import uuid4

import pytest

from models.preferences import (
    UserPreferences,
    NotificationChannel,
    NotificationPreference,
    AlertThreshold,
)


class TestNotificationPreference:
    """Tests for notification channel configuration."""

    def test_valid_email_notification(self):
        pref = NotificationPreference(
            channel=NotificationChannel.EMAIL,
            destination="user@example.com",
        )
        errors = pref.validate()
        assert errors == []

    def test_invalid_email_notification(self):
        pref = NotificationPreference(
            channel=NotificationChannel.EMAIL,
            destination="not-an-email",
        )
        errors = pref.validate()
        assert len(errors) == 1
        assert "Invalid email" in errors[0]

    def test_webhook_requires_url(self):
        pref = NotificationPreference(
            channel=NotificationChannel.WEBHOOK,
            destination="not-a-url",
        )
        errors = pref.validate()
        assert any("http" in e for e in errors)

    def test_enabled_channel_requires_destination(self):
        pref = NotificationPreference(
            channel=NotificationChannel.SLACK,
            enabled=True,
            destination="",
        )
        errors = pref.validate()
        assert len(errors) == 1


class TestUserPreferences:
    """Tests for the UserPreferences model."""

    def test_create_default_preferences(self):
        prefs = UserPreferences(user_id=uuid4())
        assert prefs.timezone == "UTC"
        assert prefs.locale == "en-US"
        assert prefs.items_per_page == 50
        assert prefs.alert_threshold == AlertThreshold.DAILY_DIGEST

    def test_items_per_page_clamped(self):
        prefs = UserPreferences(user_id=uuid4(), items_per_page=5)
        assert prefs.items_per_page == 10  # clamped to minimum

        prefs2 = UserPreferences(user_id=uuid4(), items_per_page=500)
        assert prefs2.items_per_page == 200  # clamped to maximum

    def test_add_notification_channel(self):
        prefs = UserPreferences(user_id=uuid4())
        prefs.add_notification_channel(
            NotificationChannel.EMAIL, "user@example.com"
        )
        assert len(prefs.notifications) == 1
        assert prefs.updated_at is not None

    def test_add_duplicate_channel_replaces(self):
        prefs = UserPreferences(user_id=uuid4())
        prefs.add_notification_channel(
            NotificationChannel.EMAIL, "old@example.com"
        )
        prefs.add_notification_channel(
            NotificationChannel.EMAIL, "new@example.com"
        )
        assert len(prefs.notifications) == 1
        assert prefs.notifications[0].destination == "new@example.com"

    def test_remove_notification_channel(self):
        prefs = UserPreferences(user_id=uuid4())
        prefs.add_notification_channel(
            NotificationChannel.SLACK, "#inventory-alerts"
        )
        removed = prefs.remove_notification_channel(NotificationChannel.SLACK)
        assert removed is True
        assert len(prefs.notifications) == 0

    def test_remove_nonexistent_channel(self):
        prefs = UserPreferences(user_id=uuid4())
        removed = prefs.remove_notification_channel(NotificationChannel.SMS)
        assert removed is False

    def test_get_active_channels(self):
        prefs = UserPreferences(user_id=uuid4())
        prefs.add_notification_channel(
            NotificationChannel.EMAIL, "a@b.com"
        )
        prefs.add_notification_channel(
            NotificationChannel.SLACK, "#alerts"
        )
        channels = prefs.get_active_channels()
        assert NotificationChannel.EMAIL in channels
        assert NotificationChannel.SLACK in channels

    def test_toggle_favorite_warehouse(self):
        prefs = UserPreferences(user_id=uuid4())
        wh_id = uuid4()
        added = prefs.toggle_favorite_warehouse(wh_id)
        assert added is True
        assert wh_id in prefs.favorite_warehouses

        removed = prefs.toggle_favorite_warehouse(wh_id)
        assert removed is False
        assert wh_id not in prefs.favorite_warehouses

    def test_validate_catches_bad_timezone(self):
        prefs = UserPreferences(user_id=uuid4(), timezone="BadZone")
        errors = prefs.validate()
        assert any("timezone" in e.lower() for e in errors)
PYEOF

# Topic H (part 2): Add .gitignore (minor config)
cat > .gitignore << 'PYEOF'
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
dist/
*.egg-info/
.eggs/
*.egg
.env
.venv
venv/
.pytest_cache/
.coverage
htmlcov/
.mypy_cache/
PYEOF

# Update api/__init__.py to include preferences router
cat > api/__init__.py << 'PYEOF'
"""API layer — FastAPI route definitions."""

from api.products import router as products_router
from api.inventory import router as inventory_router
from api.preferences import router as preferences_router

__all__ = ["products_router", "inventory_router", "preferences_router"]
PYEOF

# Update models/__init__.py to include preferences
cat > models/__init__.py << 'PYEOF'
"""Domain models for the inventory service."""

from models.product import Product, ProductCategory
from models.warehouse import Warehouse, StorageZone
from models.inventory import InventoryItem, StockLevel
from models.preferences import UserPreferences, NotificationChannel

__all__ = [
    "Product",
    "ProductCategory",
    "Warehouse",
    "StorageZone",
    "InventoryItem",
    "StockLevel",
    "UserPreferences",
    "NotificationChannel",
]
PYEOF

git add tests/test_preferences.py .gitignore api/__init__.py models/__init__.py
git commit -m "Add preferences tests, update module exports, add gitignore

- Comprehensive tests for UserPreferences and NotificationPreference
- Register preferences router and model in package __init__
- Add standard Python .gitignore"

echo "=== Commit 6 done (preferences tests + config) ==="

###############################################################################
# COMMIT 7: Single-topic — test infrastructure completion (topic g, part 3)
###############################################################################

# Topic G (part 3): Add test utilities module
cat > tests/test_helpers.py << 'PYEOF'
"""Test helper utilities and builders.

Provides factory functions for creating test data with sensible defaults.
Use these instead of manually constructing models in every test.
"""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from models.product import Product, ProductCategory, ProductDimensions
from models.warehouse import Warehouse, StorageZone, ZoneType
from models.inventory import InventoryItem


def make_product(
    sku: str = "TEST-001",
    name: str = "Test Product",
    category: ProductCategory = ProductCategory.GENERAL,
    length: float = 10.0,
    width: float = 5.0,
    height: float = 3.0,
    weight: float = 0.5,
    **kwargs,
) -> Product:
    """Create a Product with sensible defaults."""
    dims = ProductDimensions(
        length=length, width=width, height=height, weight=weight
    )
    return Product(sku=sku, name=name, category=category, dimensions=dims, **kwargs)


def make_zone(
    name: str = "Test Zone",
    zone_type: ZoneType = ZoneType.STANDARD,
    capacity: int = 500,
    warehouse_id: UUID | None = None,
    occupancy: int = 0,
) -> StorageZone:
    """Create a StorageZone with sensible defaults."""
    return StorageZone(
        name=name,
        zone_type=zone_type,
        capacity=capacity,
        warehouse_id=warehouse_id or uuid4(),
        current_occupancy=occupancy,
    )


def make_warehouse(
    name: str = "Test Warehouse",
    location: str = "Test City",
    zone_configs: list[tuple[str, ZoneType, int]] | None = None,
) -> Warehouse:
    """Create a Warehouse with optional zone configs.

    Args:
        zone_configs: List of (name, type, capacity) tuples for zones.
    """
    wh = Warehouse(name=name, location=location)
    if zone_configs:
        wh.zones = [
            StorageZone(
                name=zname, zone_type=ztype, capacity=cap, warehouse_id=wh.id
            )
            for zname, ztype, cap in zone_configs
        ]
    return wh


def make_inventory_item(
    product_id: UUID | None = None,
    zone_id: UUID | None = None,
    quantity: int = 100,
    lot_number: str = "LOT-TEST-001",
    days_until_expiry: int | None = None,
) -> InventoryItem:
    """Create an InventoryItem with sensible defaults."""
    expiry = None
    if days_until_expiry is not None:
        expiry = datetime.utcnow() + timedelta(days=days_until_expiry)
    return InventoryItem(
        product_id=product_id or uuid4(),
        zone_id=zone_id or uuid4(),
        quantity=quantity,
        lot_number=lot_number,
        expiry_date=expiry,
    )
PYEOF

git add tests/test_helpers.py
git commit -m "Add test helper factories for cleaner test setup

Builder functions for Product, Warehouse, StorageZone, and InventoryItem
with sensible defaults to reduce boilerplate in tests."

echo "=== Commit 7 done (test helpers) ==="

###############################################################################
# COMMIT 8: Entangled — cache usage in existing code (topic c, part 2) +
#            update __init__ for cache export (topic c)
###############################################################################

# Topic C (part 2): Wire cache into existing database queries
cat > database/cached_queries.py << 'PYEOF'
"""Cached wrappers around repository methods.

These functions add a caching layer on top of the raw repository queries.
Cache invalidation happens on write operations.
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from database.queries import ProductRepository, InventoryRepository
from utils.cache import get_cache
from utils.formatting import log_context


_cache = get_cache()


class CachedProductRepository:
    """ProductRepository with transparent caching."""

    def __init__(self, session: Session):
        self._repo = ProductRepository(session)

    def get_by_id(self, product_id: UUID) -> Optional[dict]:
        """Get product by ID with caching."""
        cache_key = f"product:{product_id}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

        result = self._repo.get_by_id(product_id)
        if result is not None:
            _cache.set(cache_key, result, ttl=600)
        return result

    def get_by_sku(self, sku: str) -> Optional[dict]:
        """Get product by SKU with caching."""
        cache_key = f"product:sku:{sku}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

        result = self._repo.get_by_sku(sku)
        if result is not None:
            _cache.set(cache_key, result, ttl=600)
        return result

    def invalidate(self, product_id: UUID, sku: Optional[str] = None) -> None:
        """Invalidate cache entries for a product."""
        _cache.delete(f"product:{product_id}")
        if sku:
            _cache.delete(f"product:sku:{sku}")


class CachedInventoryRepository:
    """InventoryRepository with caching for read-heavy operations."""

    def __init__(self, session: Session):
        self._repo = InventoryRepository(session)

    def get_total_quantity(self, product_id: UUID) -> int:
        """Get total quantity with short TTL cache."""
        cache_key = f"stock:total:{product_id}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

        result = self._repo.get_total_quantity(product_id)
        _cache.set(cache_key, result, ttl=30)  # Short TTL for stock data
        return result

    def invalidate_stock(self, product_id: UUID) -> None:
        """Invalidate stock cache after a movement."""
        prefix = log_context("cache", "invalidate_stock", str(product_id))
        _cache.delete(f"stock:total:{product_id}")
PYEOF

# Update database __init__ to export cached repos
cat > database/__init__.py << 'PYEOF'
"""Database layer for the inventory service."""

from database.connection import get_engine, get_session
from database.queries import ProductRepository, InventoryRepository, WarehouseRepository
from database.cached_queries import CachedProductRepository, CachedInventoryRepository

__all__ = [
    "get_engine",
    "get_session",
    "ProductRepository",
    "InventoryRepository",
    "WarehouseRepository",
    "CachedProductRepository",
    "CachedInventoryRepository",
]
PYEOF

# Update utils __init__ to export cache
cat > utils/__init__.py << 'PYEOF'
"""Shared utilities for the inventory service."""

from utils.formatting import format_sku, format_quantity, slugify, log_context
from utils.validation import validate_email, validate_uuid_string
from utils.cache import TTLCache, get_cache

__all__ = [
    "format_sku",
    "format_quantity",
    "slugify",
    "log_context",
    "validate_email",
    "validate_uuid_string",
    "TTLCache",
    "get_cache",
]
PYEOF

git add database/cached_queries.py database/__init__.py utils/__init__.py
git commit -m "Wire caching into database repositories and update exports

- CachedProductRepository and CachedInventoryRepository wrappers
- Export cache and WarehouseRepository from package __init__ files"

echo "=== Commit 8 done (cache integration) ==="

echo ""
echo "=== All commits created on mega-pr branch ==="
echo ""
git log --oneline --graph main..mega-pr
echo ""
echo "Files changed vs main:"
git diff --stat main..mega-pr
