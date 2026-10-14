from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DATABASE_PATH = ROOT / os.getenv("BYSG_DATABASE", "data/bysg.sqlite3")
DB_LOCK = threading.RLock()

CATEGORY_SIZES = {
    "REM": ("4", "6", "8", "10", "12", "14", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL"),
    "BUZ": ("4", "6", "8", "10", "12", "14", "XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL"),
    "GOR": ("Talle único ajustable",),
}

CATEGORY_DEFINITIONS = {
    "REM": {"name": "Remeras", "label": "Remera negra", "size": "A elegir"},
    "BUZ": {"name": "Buzos", "label": "Buzo canguro negro", "size": "A elegir"},
    "GOR": {"name": "Gorras", "label": "Gorra trucker negra", "size": "Talle único ajustable"},
}

DELIVERY_OPTIONS = {
    "pickup": {"method": "pickup", "label": "Retiro coordinado", "env": None},
    "home_delivery": {"method": "local", "label": "Entrega a domicilio", "env": "BYSG_SHIPPING_HOME"},
    "mercado_envios": {"method": "mail", "label": "Mercado Envíos", "env": "BYSG_SHIPPING_MERCADO_ENVIOS"},
    "correo_argentino": {"method": "mail", "label": "Correo Argentino", "env": "BYSG_SHIPPING_CORREO_ARGENTINO"},
    "courier": {"method": "mail", "label": "Courier a coordinar", "env": "BYSG_SHIPPING_COURIER"},
    "international_key_west": {"method": "mail", "label": "Envío internacional a Key West", "env": "BYSG_SHIPPING_KEY_WEST"},
    # Compatibilidad con pedidos creados antes de ampliar las modalidades.
    "local": {"method": "local", "label": "Entrega a domicilio", "env": "BYSG_SHIPPING_HOME"},
    "mail": {"method": "mail", "label": "Correo con seguimiento", "env": "BYSG_SHIPPING_MAIL"},
}

FINANCIAL_DEFAULTS = {
    "REM": {"finished_product_cents": 2_500_000, "bag_cents": 70_000, "keychain_cents": 200_000, "other_cost_cents": 0},
    "BUZ": {"finished_product_cents": 3_500_000, "bag_cents": 90_000, "keychain_cents": 200_000, "other_cost_cents": 0},
    "GOR": {"finished_product_cents": 1_500_000, "bag_cents": 0, "keychain_cents": 0, "other_cost_cents": 0},
}

DESCRIPTIONS = [
    "Premium minimal, con aplicación frontal y espalda.",
    "Street skater en blanco, negro y amarillo.",
    "Escudo e-sports con estética gamer.",
    "Gráfica pixel de estilo 8-bit.",
    "Sticker pack urbano con detalles gamer.",
    "Gráfica urbana de alto contraste.",
    "Composición neón violeta y celeste.",
    "Insignia gamer con terminación premium.",
    "Mascota gamer en paleta neón.",
    "Diseño dinámico en violeta y celeste.",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _price_for(category_code: str) -> int:
    defaults = {"REM": 2_500_000, "BUZ": 4_800_000, "GOR": 2_000_000}
    env_names = {"REM": "BYSG_PRICE_REMERAS", "BUZ": "BYSG_PRICE_BUZOS", "GOR": "BYSG_PRICE_GORRAS"}
    return max(0, int(os.getenv(env_names[category_code], defaults[category_code])))


def connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=20, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 20000")
    return connection


@contextmanager
def transaction() -> Iterable[sqlite3.Connection]:
    with DB_LOCK:
        connection = connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            if connection.in_transaction:
                connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


def initialize_database(reset: bool = False) -> None:
    if reset and DATABASE_PATH.exists():
        DATABASE_PATH.unlink()
    with connect() as schema_connection:
        schema_connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        order_columns = {row["name"] for row in schema_connection.execute("PRAGMA table_info(orders)").fetchall()}
        if "delivery_service" not in order_columns:
            schema_connection.execute("ALTER TABLE orders ADD COLUMN delivery_service TEXT NOT NULL DEFAULT ''")
        if "client_request_id" not in order_columns:
            schema_connection.execute("ALTER TABLE orders ADD COLUMN client_request_id TEXT")
        if "request_hash" not in order_columns:
            schema_connection.execute("ALTER TABLE orders ADD COLUMN request_hash TEXT")
        schema_connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client_request_id ON orders(client_request_id) WHERE client_request_id IS NOT NULL"
        )
        schema_connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_provider_order_id ON orders(provider_order_id) WHERE provider_order_id IS NOT NULL"
        )
    with transaction() as connection:
        for code, definition in CATEGORY_DEFINITIONS.items():
            connection.execute(
                "INSERT OR IGNORE INTO categories(code, name, capacity) VALUES (?, ?, 10)",
                (code, definition["name"]),
            )
        for category_code, definition in CATEGORY_DEFINITIONS.items():
            for design in range(1, 11):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO products(category_code, design_number, name, description)
                    VALUES (?, ?, ?, ?)
                    """,
                    (category_code, design, f"Diseño {design:02d}", DESCRIPTIONS[design - 1]),
                )
                product_id = connection.execute(
                    "SELECT id FROM products WHERE category_code = ? AND design_number = ?",
                    (category_code, design),
                ).fetchone()["id"]
                connection.execute(
                    """
                    INSERT OR IGNORE INTO variants(product_id, sku, size_label, price_cents, is_sample_price)
                    VALUES (?, ?, ?, ?, 1)
                    """,
                    (
                        product_id,
                        f"BYSG-{category_code}-D{design:02d}-NEG-PRE",
                        definition["size"],
                        _price_for(category_code),
                    ),
                )
            connection.execute(
                """
                UPDATE variants SET size_label = ?
                WHERE product_id IN (SELECT id FROM products WHERE category_code = ?)
                  AND size_label IN ('A confirmar', 'A elegir')
                """,
                (definition["size"], category_code),
            )
        connection.execute(
            "INSERT OR IGNORE INTO bauti_account(id, balance_cents, updated_at) VALUES (1, 0, ?)",
            (utc_now(),),
        )
        for category_code, values in FINANCIAL_DEFAULTS.items():
            connection.execute(
                """
                INSERT OR IGNORE INTO category_financials(
                  category_code, finished_product_cents, bag_cents, keychain_cents,
                  other_cost_cents, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    category_code,
                    values["finished_product_cents"],
                    values["bag_cents"],
                    values["keychain_cents"],
                    values["other_cost_cents"],
                    utc_now(),
                ),
            )
            total_cost = sum(values.values())
            connection.execute(
                "UPDATE variants SET cost_cents = COALESCE(cost_cents, ?) WHERE product_id IN (SELECT id FROM products WHERE category_code = ?)",
                (total_cost, category_code),
            )


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def catalog() -> dict[str, Any]:
    with connect() as connection:
        products = _rows(
            connection.execute(
                """
                SELECT p.id, p.category_code, c.name AS category, p.design_number, p.name,
                       p.description, p.color, v.id AS variant_id, v.sku, v.size_label,
                       v.price_cents, v.is_sample_price
                FROM products p
                JOIN categories c ON c.code = p.category_code
                JOIN variants v ON v.product_id = p.id
                WHERE p.active = 1 AND v.active = 1
                ORDER BY p.design_number, CASE p.category_code WHEN 'REM' THEN 1 WHEN 'BUZ' THEN 2 ELSE 3 END
                """
            ).fetchall()
        )
        stock = _rows(
            connection.execute(
                """
                SELECT code, name, capacity, reserved, sold,
                       capacity - reserved - sold AS available
                FROM categories ORDER BY CASE code WHEN 'REM' THEN 1 WHEN 'BUZ' THEN 2 ELSE 3 END
                """
            ).fetchall()
        )
        sample_prices = connection.execute(
            "SELECT COUNT(*) AS count FROM variants WHERE active = 1 AND is_sample_price = 1"
        ).fetchone()["count"]
    return {
        "products": products,
        "stock": stock,
        "currency": "ARS",
        "price_mode": "sample" if sample_prices else "final",
        "shipping": {method: _shipping_quote(method) for method in DELIVERY_OPTIONS if method not in {"local", "mail"}},
    }


def category_financials() -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT c.code, c.name, c.capacity, c.reserved, c.sold,
                   c.capacity - c.reserved - c.sold AS available,
                   f.finished_product_cents, f.bag_cents, f.keychain_cents, f.other_cost_cents,
                   f.finished_product_cents + f.bag_cents + f.keychain_cents + f.other_cost_cents AS cost_cents,
                   MIN(v.price_cents) AS price_cents, MAX(v.is_sample_price) AS is_sample_price
            FROM categories c
            JOIN category_financials f ON f.category_code = c.code
            JOIN products p ON p.category_code = c.code
            JOIN variants v ON v.product_id = p.id AND v.active = 1
            GROUP BY c.code, c.name, c.capacity, c.reserved, c.sold,
                     f.finished_product_cents, f.bag_cents, f.keychain_cents, f.other_cost_cents
            ORDER BY CASE c.code WHEN 'REM' THEN 1 WHEN 'BUZ' THEN 2 ELSE 3 END
            """
        ).fetchall()
    result = _rows(rows)
    for row in result:
        row["margin_cents"] = row["price_cents"] - row["cost_cents"]
        row["margin_percent"] = round((row["margin_cents"] / row["price_cents"] * 100), 2) if row["price_cents"] else 0
    return result


def update_category_financials(payload: dict[str, Any]) -> dict[str, Any]:
    code = str(payload.get("code") or "").upper()
    if code not in CATEGORY_DEFINITIONS:
        raise ValueError("Categoría inválida.")
    integer_fields = (
        "finished_product_cents", "bag_cents", "keychain_cents", "other_cost_cents",
        "price_cents", "capacity",
    )
    values: dict[str, int] = {}
    for field in integer_fields:
        try:
            values[field] = int(payload.get(field, 0))
        except (TypeError, ValueError) as error:
            raise ValueError(f"El valor de {field} no es válido.") from error
        if values[field] < 0:
            raise ValueError("Los importes y cupos no pueden ser negativos.")
    is_sample_price = 1 if bool(payload.get("is_sample_price", False)) else 0
    now = utc_now()
    with transaction() as connection:
        category = connection.execute("SELECT reserved, sold FROM categories WHERE code = ?", (code,)).fetchone()
        if not category:
            raise ValueError("Categoría inexistente.")
        minimum_capacity = category["reserved"] + category["sold"]
        if values["capacity"] < minimum_capacity:
            raise ValueError(f"El cupo no puede ser menor que las {minimum_capacity} unidades reservadas o vendidas.")
        previous = connection.execute("SELECT capacity FROM categories WHERE code = ?", (code,)).fetchone()["capacity"]
        connection.execute("UPDATE categories SET capacity = ? WHERE code = ?", (values["capacity"], code))
        connection.execute(
            """
            INSERT INTO category_financials(
              category_code, finished_product_cents, bag_cents, keychain_cents, other_cost_cents, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(category_code) DO UPDATE SET
              finished_product_cents = excluded.finished_product_cents,
              bag_cents = excluded.bag_cents,
              keychain_cents = excluded.keychain_cents,
              other_cost_cents = excluded.other_cost_cents,
              updated_at = excluded.updated_at
            """,
            (
                code, values["finished_product_cents"], values["bag_cents"],
                values["keychain_cents"], values["other_cost_cents"], now,
            ),
        )
        total_cost = sum(values[field] for field in ("finished_product_cents", "bag_cents", "keychain_cents", "other_cost_cents"))
        connection.execute(
            """
            UPDATE variants SET cost_cents = ?, price_cents = ?, is_sample_price = ?
            WHERE product_id IN (SELECT id FROM products WHERE category_code = ?)
            """,
            (total_cost, values["price_cents"], is_sample_price, code),
        )
        connection.execute(
            """
            INSERT INTO stock_ledger(category_code, event_type, detail, created_at)
            VALUES (?, 'admin_configuration', ?, ?)
            """,
            (code, f"Costos, precio y cupo actualizados. Cupo {previous} → {values['capacity']}", now),
        )
    return next(row for row in category_financials() if row["code"] == code)


def stock_status(connection: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    own_connection = connection is None
    connection = connection or connect()
    try:
        return _rows(
            connection.execute(
                """
                SELECT code, name, capacity, reserved, sold,
                       capacity - reserved - sold AS available
                FROM categories ORDER BY CASE code WHEN 'REM' THEN 1 WHEN 'BUZ' THEN 2 ELSE 3 END
                """
            ).fetchall()
        )
    finally:
        if own_connection:
            connection.close()


def _shipping_quote(delivery_option: str) -> int | None:
    definition = DELIVERY_OPTIONS[delivery_option]
    if definition["method"] == "pickup":
        return 0
    env_name = definition["env"]
    raw_value = os.getenv(env_name, "").strip() if env_name else ""
    if not raw_value:
        return None
    try:
        cents = int(raw_value)
    except ValueError:
        return None
    return cents if cents >= 0 else None


def _checkout_request_hash(payload: dict[str, Any], provider: str, client_request_id: str) -> str:
    try:
        parsed_request_id = uuid.UUID(client_request_id)
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("El identificador de la solicitud no es válido.") from error
    if parsed_request_id.version != 4 or str(parsed_request_id) != client_request_id:
        raise ValueError("El identificador de la solicitud no es válido.")
    canonical = json.dumps({"provider": provider, "payload": payload}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_existing_order_for_request(payload: dict[str, Any], provider: str, client_request_id: str) -> dict[str, Any] | None:
    """Resolve a browser retry before any external identity call or new reservation."""
    request_hash = _checkout_request_hash(payload, provider, client_request_id)
    with connect() as connection:
        existing = connection.execute(
            "SELECT public_token, request_hash FROM orders WHERE client_request_id = ?",
            (client_request_id,),
        ).fetchone()
    if existing is None:
        return None
    if existing["request_hash"] != request_hash:
        raise ValueError("La solicitud ya corresponde a otro pedido. Recargá la página para comenzar uno nuevo.")
    return get_order(existing["public_token"], include_private=True)


def create_order(
    payload: dict[str, Any],
    provider: str = "mock",
    *,
    client_request_id: str | None = None,
    return_created: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], bool]:
    request_hash = _checkout_request_hash(payload, provider, client_request_id) if client_request_id is not None else None
    customer = payload.get("customer") or {}
    raw_items = payload.get("items") or []
    delivery_option = str(payload.get("delivery_method") or "")
    if delivery_option not in DELIVERY_OPTIONS:
        raise ValueError("Elegí una forma de entrega válida.")
    delivery_definition = DELIVERY_OPTIONS[delivery_option]
    delivery_method = delivery_definition["method"]
    delivery_service = delivery_definition["label"]
    delivery_address = str(payload.get("delivery_address") or "").strip()[:300]
    if delivery_method != "pickup" and len(delivery_address) < 5:
        raise ValueError("Completá la dirección de entrega.")
    required = {"name": 2, "email": 5, "phone": 6}
    for field, minimum in required.items():
        if len(str(customer.get(field) or "").strip()) < minimum:
            raise ValueError(f"Falta completar {field}.")
    email = str(customer["email"]).strip().lower()
    if "@" not in email or len(email) > 160:
        raise ValueError("El correo electrónico no es válido.")
    if not raw_items or len(raw_items) > 30:
        raise ValueError("El pedido no tiene productos válidos.")

    normalized: dict[tuple[int, str], int] = {}
    for item in raw_items:
        variant_id = int(item.get("variant_id") or 0)
        quantity = int(item.get("quantity") or 0)
        size_label = str(item.get("size_label") or "").strip()
        if variant_id < 1 or quantity < 1 or quantity > 10:
            raise ValueError("Hay una cantidad o variante inválida.")
        if not size_label:
            raise ValueError("Elegí el talle del producto.")
        key = (variant_id, size_label)
        normalized[key] = normalized.get(key, 0) + quantity

    now = utc_now()
    public_token = secrets.token_urlsafe(24)
    with transaction() as connection:
        if client_request_id is not None:
            existing = connection.execute(
                "SELECT public_token, request_hash FROM orders WHERE client_request_id = ?",
                (client_request_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise ValueError("La solicitud ya corresponde a otro pedido. Recargá la página para comenzar uno nuevo.")
                order = get_order(existing["public_token"], include_private=True)
                return (order, False) if return_created else order
        variant_ids = sorted({variant_id for variant_id, _ in normalized})
        placeholders = ",".join("?" for _ in variant_ids)
        variant_rows = connection.execute(
            f"""
            SELECT v.id AS variant_id, v.sku, v.size_label, v.price_cents, v.is_sample_price,
                   p.name, p.design_number, p.category_code, c.name AS category_name
            FROM variants v
            JOIN products p ON p.id = v.product_id
            JOIN categories c ON c.code = p.category_code
            WHERE v.active = 1 AND p.active = 1 AND v.id IN ({placeholders})
            """,
            tuple(variant_ids),
        ).fetchall()
        if len(variant_rows) != len(variant_ids):
            raise ValueError("Uno de los productos ya no está disponible.")
        variants = {row["variant_id"]: dict(row) for row in variant_rows}
        category_quantities: dict[str, int] = {}
        subtotal = 0
        order_lines: list[tuple[dict[str, Any], str, int]] = []
        for (variant_id, requested_size), quantity in normalized.items():
            variant = variants[variant_id]
            if provider == "mercado_pago" and variant["is_sample_price"]:
                raise ValueError("El precio de un producto todavía es de muestra. Aprobalo antes de cobrar.")
            if provider == "mercado_pago" and variant["price_cents"] <= 0:
                raise ValueError("El precio de un producto debe ser mayor a cero antes de cobrar.")
            allowed_sizes = CATEGORY_SIZES[variant["category_code"]]
            size_label = next((size for size in allowed_sizes if size.casefold() == requested_size.casefold()), None)
            if not size_label:
                raise ValueError(f"El talle indicado no corresponde a {variant['category_name'].lower()}.")
            order_lines.append((variant, size_label, quantity))
            category_quantities[variant["category_code"]] = category_quantities.get(variant["category_code"], 0) + quantity
            subtotal += variant["price_cents"] * quantity
        for category_code, quantity in category_quantities.items():
            category = connection.execute(
                "SELECT capacity, reserved, sold, name FROM categories WHERE code = ?",
                (category_code,),
            ).fetchone()
            available = category["capacity"] - category["reserved"] - category["sold"]
            if quantity > available:
                raise ValueError(f"Solo quedan {available} cupos de {category['name'].lower()}.")

        shipping_quote = _shipping_quote(delivery_option)
        if shipping_quote is None:
            raise ValueError("El costo de entrega todavía no está definido. Configuralo antes de crear el pedido.")
        shipping = shipping_quote
        total = subtotal + shipping
        cursor = connection.execute(
            """
            INSERT INTO orders(
              public_token, order_number, customer_name, customer_email, customer_phone,
              delivery_method, delivery_service, delivery_address, notes, subtotal_cents, shipping_cents,
              total_cents, provider, client_request_id, request_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                public_token,
                "TEMP",
                str(customer["name"]).strip()[:120],
                email,
                str(customer["phone"]).strip()[:50],
                delivery_method,
                delivery_service,
                delivery_address,
                str(payload.get("notes") or "").strip()[:500],
                subtotal,
                shipping,
                total,
                provider,
                client_request_id,
                request_hash,
                now,
                now,
            ),
        )
        order_id = cursor.lastrowid
        order_number = f"BYSG-{datetime.now().strftime('%Y%m')}-{order_id:05d}"
        connection.execute("UPDATE orders SET order_number = ? WHERE id = ?", (order_number, order_id))
        for variant, size_label, quantity in order_lines:
            variant_id = variant["variant_id"]
            title = f"{CATEGORY_DEFINITIONS[variant['category_code']]['label']} · {variant['name']}"
            line_total = variant["price_cents"] * quantity
            connection.execute(
                """
                INSERT INTO order_items(
                  order_id, variant_id, sku, title, category_code, design_number,
                  size_label, quantity, unit_price_cents, line_total_cents
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    variant_id,
                    variant["sku"],
                    title,
                    variant["category_code"],
                    variant["design_number"],
                    size_label,
                    quantity,
                    variant["price_cents"],
                    line_total,
                ),
            )
        for category_code, quantity in category_quantities.items():
            connection.execute("UPDATE categories SET reserved = reserved + ? WHERE code = ?", (quantity, category_code))
            connection.execute(
                """
                INSERT INTO stock_ledger(category_code, order_id, event_type, reserved_delta, detail, created_at)
                VALUES (?, ?, 'order_reserved', ?, ?, ?)
                """,
                (category_code, order_id, quantity, f"Reserva {order_number}", now),
            )
        connection.execute(
            "INSERT INTO payments(order_id, provider, status, amount_cents, created_at, updated_at) VALUES (?, ?, 'pending', ?, ?, ?)",
            (order_id, provider, total, now, now),
        )
        connection.execute(
            "INSERT INTO order_events(order_id, event_type, detail, created_at) VALUES (?, 'order_created', 'Pedido creado y cupos reservados', ?)",
            (order_id, now),
        )
    order = get_order(public_token, include_private=True)
    return (order, True) if return_created else order


def create_or_get_order(payload: dict[str, Any], provider: str, client_request_id: str) -> tuple[dict[str, Any], bool]:
    """Create one local order per browser attempt, even after a lost response."""
    result = create_order(payload, provider=provider, client_request_id=client_request_id, return_created=True)
    return result


def get_order(public_token: str, include_private: bool = False) -> dict[str, Any]:
    with connect() as connection:
        row = connection.execute("SELECT * FROM orders WHERE public_token = ?", (public_token,)).fetchone()
        if not row:
            raise KeyError("Pedido no encontrado")
        order = dict(row)
        order["items"] = _rows(connection.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (row["id"],)).fetchall())
        order["events"] = _rows(connection.execute("SELECT event_type, detail, created_at FROM order_events WHERE order_id = ? ORDER BY id", (row["id"],)).fetchall())
        if not include_private:
            order.pop("id", None)
            order.pop("client_request_id", None)
            order.pop("request_hash", None)
            order["customer_email"] = _mask_email(order["customer_email"])
            order["customer_phone"] = _mask_phone(order["customer_phone"])
        return order


def _mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:2]}***@{domain}" if domain else "***"


def _mask_phone(phone: str) -> str:
    return f"***{phone[-4:]}" if len(phone) >= 4 else "***"


def attach_provider_order(public_token: str, provider_order_id: str) -> None:
    if not provider_order_id or len(provider_order_id) > 100:
        raise ValueError("Identificador remoto de pedido inválido.")
    with transaction() as connection:
        order = connection.execute(
            "SELECT provider_order_id, payment_status FROM orders WHERE public_token = ?",
            (public_token,),
        ).fetchone()
        if order is None:
            raise KeyError("Pedido no encontrado")
        if order["payment_status"] != "pending":
            raise ValueError("Solo se puede vincular un pedido con pago pendiente.")
        if order["provider_order_id"] is not None and order["provider_order_id"] != provider_order_id:
            raise ValueError("El pedido ya está vinculado a otra order de Mercado Pago.")
        if order["provider_order_id"] is None:
            connection.execute(
                "UPDATE orders SET provider_order_id = ?, updated_at = ? WHERE public_token = ? AND provider_order_id IS NULL",
                (provider_order_id, utc_now(), public_token),
            )


def apply_payment_status(
    public_token: str,
    status: str,
    provider_payment_id: str,
    raw_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in {"pending", "approved", "rejected", "refunded"}:
        raise ValueError("Estado de pago inválido")
    now = utc_now()
    with transaction() as connection:
        order = connection.execute("SELECT * FROM orders WHERE public_token = ?", (public_token,)).fetchone()
        if not order:
            raise KeyError("Pedido no encontrado")
        current = order["payment_status"]
        if current == status:
            return get_order(public_token, include_private=True)
        if current == "refunded" or (current == "approved" and status in {"pending", "rejected"}) or (current == "rejected" and status == "pending"):
            return get_order(public_token, include_private=True)
        quantities = _rows(
            connection.execute(
                "SELECT category_code, SUM(quantity) AS quantity FROM order_items WHERE order_id = ? GROUP BY category_code",
                (order["id"],),
            ).fetchall()
        )
        order_status = order["order_status"]
        if status == "approved" and current in {"pending", "rejected"}:
            for item in quantities:
                reserved_delta = -item["quantity"] if current == "pending" else 0
                if current == "rejected":
                    capacity = connection.execute(
                        "SELECT capacity - reserved - sold AS available FROM categories WHERE code = ?",
                        (item["category_code"],),
                    ).fetchone()["available"]
                    if capacity < item["quantity"]:
                        raise ValueError("Pago tardío sin cupo disponible; requiere revisión antes de confirmar.")
                connection.execute(
                    "UPDATE categories SET reserved = reserved + ?, sold = sold + ? WHERE code = ?",
                    (reserved_delta, item["quantity"], item["category_code"]),
                )
                connection.execute(
                    """
                    INSERT INTO stock_ledger(category_code, order_id, event_type, reserved_delta, sold_delta, detail, created_at)
                    VALUES (?, ?, 'payment_approved', ?, ?, ?, ?)
                    """,
                    (item["category_code"], order["id"], reserved_delta, item["quantity"], f"Pago aprobado {order['order_number']}", now),
                )
            order_status = "paid"
        elif status == "rejected" and current == "pending":
            for item in quantities:
                connection.execute(
                    "UPDATE categories SET reserved = reserved - ? WHERE code = ?",
                    (item["quantity"], item["category_code"]),
                )
                connection.execute(
                    """
                    INSERT INTO stock_ledger(category_code, order_id, event_type, reserved_delta, detail, created_at)
                    VALUES (?, ?, 'payment_rejected', ?, ?, ?)
                    """,
                    (item["category_code"], order["id"], -item["quantity"], f"Cupo liberado {order['order_number']}", now),
                )
            order_status = "payment_rejected"
        elif status == "refunded" and current == "approved":
            for item in quantities:
                connection.execute(
                    "UPDATE categories SET sold = sold - ? WHERE code = ?",
                    (item["quantity"], item["category_code"]),
                )
                connection.execute(
                    """
                    INSERT INTO stock_ledger(category_code, order_id, event_type, sold_delta, detail, created_at)
                    VALUES (?, ?, 'payment_refunded', ?, ?, ?)
                    """,
                    (item["category_code"], order["id"], -item["quantity"], f"Stock repuesto {order['order_number']}", now),
                )
            order_status = "cancelled"
        elif status == "refunded" and current in {"pending", "rejected"}:
            if current == "pending":
                for item in quantities:
                    connection.execute(
                        "UPDATE categories SET reserved = reserved - ? WHERE code = ?",
                        (item["quantity"], item["category_code"]),
                    )
                    connection.execute(
                        """INSERT INTO stock_ledger(category_code, order_id, event_type, reserved_delta, detail, created_at)
                        VALUES (?, ?, 'payment_refunded', ?, ?, ?)""",
                        (item["category_code"], order["id"], -item["quantity"], f"Cupo liberado {order['order_number']}", now),
                    )
            order_status = "cancelled"
        connection.execute(
            "UPDATE orders SET payment_status = ?, order_status = ?, updated_at = ? WHERE id = ?",
            (status, order_status, now, order["id"]),
        )
        connection.execute(
            """
            UPDATE payments SET provider_payment_id = COALESCE(provider_payment_id, ?),
              status = ?, raw_payload = ?, updated_at = ? WHERE order_id = ?
            """,
            (provider_payment_id, status, json.dumps(raw_payload or {}, ensure_ascii=False), now, order["id"]),
        )
        connection.execute(
            "INSERT INTO order_events(order_id, event_type, detail, created_at) VALUES (?, ?, ?, ?)",
            (order["id"], f"payment_{status}", f"Pago {status}", now),
        )
        if status == "approved":
            balance = connection.execute("SELECT balance_cents FROM bauti_account WHERE id = 1").fetchone()["balance_cents"]
            new_balance = balance + order["total_cents"]
            connection.execute("UPDATE bauti_account SET balance_cents = ?, updated_at = ? WHERE id = 1", (new_balance, now))
            connection.execute(
                """
                INSERT INTO bauti_ledger(order_id, concept, credit_cents, balance_cents, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (order["id"], f"Venta {order['order_number']}", order["total_cents"], new_balance, now),
            )
        elif status == "refunded" and current == "approved":
            balance = connection.execute("SELECT balance_cents FROM bauti_account WHERE id = 1").fetchone()["balance_cents"]
            new_balance = balance - order["total_cents"]
            connection.execute("UPDATE bauti_account SET balance_cents = ?, updated_at = ? WHERE id = 1", (new_balance, now))
            connection.execute(
                """INSERT INTO bauti_ledger(order_id, concept, debit_cents, balance_cents, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (order["id"], f"Devolución {order['order_number']}", order["total_cents"], new_balance, now),
            )
    return get_order(public_token, include_private=True)


def set_order_status(public_token: str, status: str, tracking: dict[str, Any] | None = None) -> dict[str, Any]:
    allowed = {"paid", "in_production", "ready_for_pickup", "shipped", "delivered", "cancelled"}
    if status not in allowed:
        raise ValueError("Estado de pedido inválido")
    tracking = tracking or {}
    now = utc_now()
    with transaction() as connection:
        order = connection.execute("SELECT id, payment_status, order_status, order_number FROM orders WHERE public_token = ?", (public_token,)).fetchone()
        if not order:
            raise KeyError("Pedido no encontrado")
        if status not in {"cancelled"} and order["payment_status"] != "approved":
            raise ValueError("El pedido debe estar pagado antes de avanzar.")
        if status == "cancelled" and order["payment_status"] == "pending" and order["order_status"] != "cancelled":
            quantities = connection.execute(
                "SELECT category_code, SUM(quantity) AS quantity FROM order_items WHERE order_id = ? GROUP BY category_code",
                (order["id"],),
            ).fetchall()
            for item in quantities:
                connection.execute(
                    "UPDATE categories SET reserved = reserved - ? WHERE code = ?",
                    (item["quantity"], item["category_code"]),
                )
                connection.execute(
                    """
                    INSERT INTO stock_ledger(category_code, order_id, event_type, reserved_delta, detail, created_at)
                    VALUES (?, ?, 'order_cancelled', ?, ?, ?)
                    """,
                    (item["category_code"], order["id"], -item["quantity"], f"Cupo liberado {order['order_number']}", now),
                )
            connection.execute("UPDATE payments SET status = 'rejected', updated_at = ? WHERE order_id = ?", (now, order["id"]))
            connection.execute("UPDATE orders SET payment_status = 'rejected' WHERE id = ?", (order["id"],))
        connection.execute(
            """
            UPDATE orders SET order_status = ?, tracking_provider = ?, tracking_code = ?, tracking_url = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                str(tracking.get("provider") or "")[:80] or None,
                str(tracking.get("code") or "")[:120] or None,
                str(tracking.get("url") or "")[:500] or None,
                now,
                order["id"],
            ),
        )
        connection.execute(
            "INSERT INTO order_events(order_id, event_type, detail, created_at) VALUES (?, 'order_status', ?, ?)",
            (order["id"], f"Estado actualizado a {status}", now),
        )
    return get_order(public_token, include_private=True)


def list_orders(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as connection:
        return _rows(
            connection.execute(
                """
                SELECT o.*, COALESCE(SUM(oi.quantity), 0) AS units
                FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.id
                GROUP BY o.id ORDER BY o.id DESC LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        )


def dashboard() -> dict[str, Any]:
    with connect() as connection:
        metrics = dict(
            connection.execute(
                """
                SELECT COUNT(*) AS orders,
                       COALESCE(SUM(CASE WHEN payment_status = 'approved' THEN 1 ELSE 0 END), 0) AS paid_orders,
                       COALESCE(SUM(CASE WHEN payment_status = 'approved' THEN total_cents ELSE 0 END), 0) AS revenue_cents,
                       COALESCE(SUM(CASE WHEN payment_status = 'pending' THEN total_cents ELSE 0 END), 0) AS pending_cents
                FROM orders
                """
            ).fetchone()
        )
        account = dict(connection.execute("SELECT * FROM bauti_account WHERE id = 1").fetchone())
        ledger = _rows(connection.execute("SELECT * FROM bauti_ledger ORDER BY id DESC LIMIT 100").fetchall())
        email_summary = dict(
            connection.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END), 0) AS queued,
                       COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed,
                       COALESCE(SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END), 0) AS sent
                FROM email_outbox
                """
            ).fetchone()
        )
        subscribers = connection.execute(
            "SELECT COUNT(*) AS count FROM newsletter_subscribers WHERE status = 'active'"
        ).fetchone()["count"]
        messages = _rows(
            connection.execute(
                "SELECT * FROM contact_messages ORDER BY id DESC LIMIT 20"
            ).fetchall()
        )
        withdrawals = _rows(
            connection.execute(
                "SELECT * FROM withdrawal_requests ORDER BY id DESC LIMIT 20"
            ).fetchall()
        )
        outbox = _rows(
            connection.execute(
                "SELECT id, recipient, subject, template_name, status, attempts, last_error, created_at, sent_at FROM email_outbox ORDER BY id DESC LIMIT 20"
            ).fetchall()
        )
    return {
        "metrics": metrics,
        "stock": stock_status(),
        "financials": category_financials(),
        "orders": list_orders(),
        "bauti_account": account,
        "bauti_ledger": ledger,
        "email_summary": email_summary,
        "email_outbox": outbox,
        "newsletter_subscribers": subscribers,
        "contact_messages": messages,
        "withdrawal_requests": withdrawals,
    }


def add_bauti_movement(concept: str, debit_cents: int = 0, credit_cents: int = 0) -> dict[str, Any]:
    if debit_cents < 0 or credit_cents < 0 or (debit_cents == 0) == (credit_cents == 0):
        raise ValueError("Informá un débito o un crédito válido.")
    now = utc_now()
    with transaction() as connection:
        current = connection.execute("SELECT balance_cents FROM bauti_account WHERE id = 1").fetchone()["balance_cents"]
        balance = current + credit_cents - debit_cents
        connection.execute("UPDATE bauti_account SET balance_cents = ?, updated_at = ? WHERE id = 1", (balance, now))
        cursor = connection.execute(
            """
            INSERT INTO bauti_ledger(concept, debit_cents, credit_cents, balance_cents, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (concept.strip()[:180], debit_cents, credit_cents, balance, now),
        )
    return {"id": cursor.lastrowid, "balance_cents": balance}


def record_webhook(provider_event_id: str, event_type: str, payload: dict[str, Any]) -> bool:
    try:
        with transaction() as connection:
            connection.execute(
                "INSERT INTO webhook_events(provider_event_id, event_type, payload, received_at) VALUES (?, ?, ?, ?)",
                (provider_event_id, event_type, json.dumps(payload, ensure_ascii=False), utc_now()),
            )
        return True
    except sqlite3.IntegrityError:
        # Receipt is not completion. A provider timeout or a process crash must
        # leave the event retryable; only successfully applied events are final.
        with connect() as connection:
            row = connection.execute(
                "SELECT processed_at FROM webhook_events WHERE provider_event_id = ?",
                (provider_event_id,),
            ).fetchone()
        return bool(row and row["processed_at"] is None)


def mark_webhook_processed(provider_event_id: str) -> None:
    with transaction() as connection:
        connection.execute("UPDATE webhook_events SET processed_at = ? WHERE provider_event_id = ?", (utc_now(), provider_event_id))


def subscribe_newsletter(email: str) -> dict[str, Any]:
    normalized = email.strip().lower()
    if "@" not in normalized or len(normalized) > 160:
        raise ValueError("Ingresá un correo electrónico válido.")
    with transaction() as connection:
        connection.execute(
            """
            INSERT INTO newsletter_subscribers(email, status, created_at) VALUES (?, 'active', ?)
            ON CONFLICT(email) DO UPDATE SET status = 'active'
            """,
            (normalized, utc_now()),
        )
    return {"status": "subscribed", "email": normalized}


def create_contact_message(name: str, email: str, message: str) -> dict[str, Any]:
    clean_name = name.strip()
    clean_email = email.strip().lower()
    clean_message = message.strip()
    if len(clean_name) < 2 or len(clean_name) > 120:
        raise ValueError("Ingresá tu nombre.")
    if "@" not in clean_email or len(clean_email) > 160:
        raise ValueError("Ingresá un correo electrónico válido.")
    if len(clean_message) < 10 or len(clean_message) > 2000:
        raise ValueError("El mensaje debe tener entre 10 y 2000 caracteres.")
    with transaction() as connection:
        cursor = connection.execute(
            "INSERT INTO contact_messages(name, email, message, created_at) VALUES (?, ?, ?, ?)",
            (clean_name, clean_email, clean_message, utc_now()),
        )
    return {"id": cursor.lastrowid, "status": "received"}


def create_withdrawal_request(order_number: str, email: str, detail: str = "") -> dict[str, Any]:
    clean_order = order_number.strip().upper()
    clean_email = email.strip().lower()
    clean_detail = detail.strip()
    if len(clean_order) < 5 or len(clean_order) > 60:
        raise ValueError("Ingresá el número de pedido.")
    if "@" not in clean_email or len(clean_email) > 160:
        raise ValueError("Ingresá un correo electrónico válido.")
    if len(clean_detail) > 1000:
        raise ValueError("El detalle es demasiado largo.")
    code = f"ARR-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
    with transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO withdrawal_requests(confirmation_code, order_number, email, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (code, clean_order, clean_email, clean_detail, utc_now()),
        )
    return {"id": cursor.lastrowid, "status": "received", "confirmation_code": code}


def queue_email(
    order: dict[str, Any], event_name: str, subject: str, template_name: str, html_body: str
) -> bool:
    event_key = f"{order['id']}:{event_name}"
    try:
        with transaction() as connection:
            connection.execute(
                """
                INSERT INTO email_outbox(
                  order_id, event_key, recipient, subject, template_name, html_body, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order["id"], event_key, order["customer_email"], subject,
                    template_name, html_body, utc_now(),
                ),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def pending_emails(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as connection:
        return _rows(
            connection.execute(
                """
                SELECT * FROM email_outbox
                WHERE status IN ('queued', 'failed') AND attempts < 5
                ORDER BY id LIMIT ?
                """,
                (max(1, min(limit, 100)),),
            ).fetchall()
        )


def mark_email_sent(email_id: int) -> None:
    with transaction() as connection:
        connection.execute(
            "UPDATE email_outbox SET status = 'sent', attempts = attempts + 1, last_error = NULL, sent_at = ? WHERE id = ?",
            (utc_now(), email_id),
        )


def mark_email_failed(email_id: int, error: str) -> None:
    with transaction() as connection:
        connection.execute(
            "UPDATE email_outbox SET status = 'failed', attempts = attempts + 1, last_error = ? WHERE id = ?",
            (error[:500], email_id),
        )
