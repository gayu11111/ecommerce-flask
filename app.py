import sqlite3
from functools import wraps

from flask import (Flask, render_template, request, g, jsonify, abort,
                   session, redirect, url_for, flash)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-secret-key"
DATABASE = "shop.db"
ORDER_STATUSES = ["Placed", "Shipped", "Delivered", "Cancelled"]


# =====================================================
#  DATABASE + LOGIN HELPERS
# =====================================================

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()


@app.before_request
def load_user():
    """Runs before every request: finds the logged-in user (if any)."""
    g.user = None
    user_id = session.get("user_id")
    if user_id:
        g.user = get_db().execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()


@app.context_processor
def inject_globals():
    """Makes `user` and `cart_count` available in every template."""
    count = 0
    if g.user:
        row = get_db().execute(
            "SELECT COALESCE(SUM(quantity), 0) AS n FROM cart_items WHERE user_id = ?",
            (g.user["id"],),
        ).fetchone()
        count = row["n"]
    return dict(user=g.user, cart_count=count)


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not g.user:
            flash("Please log in first.", "error")
            nxt = request.path if request.method == "GET" else None
            return redirect(url_for("login", next=nxt))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not g.user:
            flash("Please log in first.", "error")
            return redirect(url_for("login"))
        if not g.user["is_admin"]:
            abort(403)
        return f(*args, **kwargs)
    return wrapper


def api_admin_required(f):
    """Same as admin_required, but answers with JSON (for the API)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not g.user:
            return jsonify(error="Login required"), 401
        if not g.user["is_admin"]:
            return jsonify(error="Admin only"), 403
        return f(*args, **kwargs)
    return wrapper


# =====================================================
#  STEP 1: PRODUCT LISTING PAGES
# =====================================================

SORTS = {
    "featured": "id ASC",
    "price_asc": "price ASC",
    "price_desc": "price DESC",
    "name": "name COLLATE NOCASE ASC",
}


@app.route("/")
def index():
    db = get_db()
    search = request.args.get("search", "").strip()
    category = request.args.get("category", "")
    min_price = request.args.get("min_price", type=float)
    max_price = request.args.get("max_price", type=float)
    in_stock = request.args.get("in_stock")
    sort = request.args.get("sort", "featured")

    query = "SELECT * FROM products WHERE 1=1"
    params = []

    if search:
        query += " AND name LIKE ?"
        params.append(f"%{search}%")
    if category:
        query += " AND category = ?"
        params.append(category)
    if min_price is not None:
        query += " AND price >= ?"
        params.append(min_price)
    if max_price is not None:
        query += " AND price <= ?"
        params.append(max_price)
    if in_stock:
        query += " AND stock > 0"

    # only allow sort options from our list (keeps the SQL safe)
    query += " ORDER BY " + SORTS.get(sort, SORTS["featured"])

    products = db.execute(query, params).fetchall()
    categories = db.execute(
        "SELECT DISTINCT category FROM products ORDER BY category"
    ).fetchall()

    return render_template(
        "products.html",
        products=products,
        categories=categories,
        search=search,
        selected=category,
        min_price=request.args.get("min_price", ""),
        max_price=request.args.get("max_price", ""),
        in_stock=bool(in_stock),
        sort=sort,
    )


@app.route("/product/<int:product_id>")
def product_detail(product_id):
    db = get_db()
    product = db.execute(
        "SELECT * FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if product is None:
        abort(404)
    related = db.execute(
        "SELECT * FROM products WHERE id != ? "
        "ORDER BY (category = ?) DESC, id LIMIT 3",
        (product_id, product["category"]),
    ).fetchall()
    return render_template("product_detail.html", p=product, related=related)


# =====================================================
#  STEP 2: PRODUCT MANAGEMENT (helpers, API, admin pages)
# =====================================================

def validate_product(data, partial=False):
    """Checks product data. Returns (clean_data, error_message)."""
    fields = ["name", "description", "price", "stock", "category", "image_url"]
    clean = {k: data[k] for k in fields if k in data}

    if not partial:
        for required in ("name", "price"):
            if required not in clean or clean[required] in ("", None):
                return None, f"'{required}' is required"

    if "name" in clean and not str(clean["name"]).strip():
        return None, "'name' cannot be empty"

    if "price" in clean:
        try:
            clean["price"] = float(clean["price"])
        except (TypeError, ValueError):
            return None, "'price' must be a number"
        if clean["price"] < 0:
            return None, "'price' cannot be negative"

    if "stock" in clean:
        try:
            clean["stock"] = int(clean["stock"])
        except (TypeError, ValueError):
            return None, "'stock' must be a whole number"
        if clean["stock"] < 0:
            return None, "'stock' cannot be negative"

    return clean, None


def build_product(data):
    """Validates a full product and fills in default values."""
    clean, error = validate_product(data)
    if error:
        return None, error
    clean.setdefault("stock", 0)
    clean.setdefault("description", "")
    if not clean.get("category"):
        clean["category"] = "General"
    if not clean.get("image_url"):
        clean["image_url"] = (
            "https://placehold.co/400x300/FFE8D6/7C2D12?text="
            + str(clean["name"]).strip().replace(" ", "+")
        )
    return clean, None


def delete_product(db, product_id):
    """Deletes a product. Returns an error message, or None if it worked."""
    in_orders = db.execute(
        "SELECT 1 FROM order_items WHERE product_id = ? LIMIT 1", (product_id,)
    ).fetchone()
    if in_orders:
        return "This product is part of past orders. Set its stock to 0 instead."
    db.execute("DELETE FROM cart_items WHERE product_id = ?", (product_id,))
    db.execute("DELETE FROM products WHERE id = ?", (product_id,))
    db.commit()
    return None


# ---------- JSON API (reading is public, changes need an admin login) ----------

@app.route("/api/products", methods=["GET"])
def api_list_products():
    rows = get_db().execute("SELECT * FROM products ORDER BY id").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/products/<int:product_id>", methods=["GET"])
def api_get_product(product_id):
    row = get_db().execute(
        "SELECT * FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if row is None:
        return jsonify(error="Product not found"), 404
    return jsonify(dict(row))


@app.route("/api/products", methods=["POST"])
@api_admin_required
def api_create_product():
    clean, error = build_product(request.get_json(silent=True) or {})
    if error:
        return jsonify(error=error), 400

    db = get_db()
    cur = db.execute(
        "INSERT INTO products (name, description, price, stock, category, image_url) "
        "VALUES (:name, :description, :price, :stock, :category, :image_url)",
        clean,
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM products WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/products/<int:product_id>", methods=["PUT"])
@api_admin_required
def api_update_product(product_id):
    db = get_db()
    if db.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone() is None:
        return jsonify(error="Product not found"), 404

    clean, error = validate_product(request.get_json(silent=True) or {}, partial=True)
    if error:
        return jsonify(error=error), 400
    if not clean:
        return jsonify(error="Nothing to update"), 400

    set_clause = ", ".join(f"{k} = :{k}" for k in clean)
    clean["id"] = product_id
    db.execute(f"UPDATE products SET {set_clause} WHERE id = :id", clean)
    db.commit()
    row = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    return jsonify(dict(row))


@app.route("/api/products/<int:product_id>", methods=["DELETE"])
@api_admin_required
def api_delete_product(product_id):
    db = get_db()
    if db.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone() is None:
        return jsonify(error="Product not found"), 404
    error = delete_product(db, product_id)
    if error:
        return jsonify(error=error), 409
    return jsonify(message="Product deleted")


# ---------- Admin pages ----------

def form_data():
    """Reads the HTML form into a clean dictionary."""
    data = {k: v.strip() for k, v in request.form.items()}
    if data.get("stock") == "":
        data["stock"] = 0
    return data


@app.route("/admin")
@admin_required
def admin_products():
    products = get_db().execute("SELECT * FROM products ORDER BY id").fetchall()
    return render_template("admin_products.html", products=products)


@app.route("/admin/add", methods=["GET", "POST"])
@admin_required
def admin_add():
    if request.method == "POST":
        data = form_data()
        clean, error = build_product(data)
        if error:
            flash(error, "error")
            return render_template("admin_form.html", product=data, editing=False)
        db = get_db()
        db.execute(
            "INSERT INTO products (name, description, price, stock, category, image_url) "
            "VALUES (:name, :description, :price, :stock, :category, :image_url)",
            clean,
        )
        db.commit()
        flash("Product added.", "success")
        return redirect(url_for("admin_products"))
    return render_template("admin_form.html", product={}, editing=False)


@app.route("/admin/edit/<int:product_id>", methods=["GET", "POST"])
@admin_required
def admin_edit(product_id):
    db = get_db()
    product = db.execute(
        "SELECT * FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if product is None:
        abort(404)

    if request.method == "POST":
        data = form_data()
        clean, error = build_product(data)
        if error:
            flash(error, "error")
            data["id"] = product_id
            return render_template("admin_form.html", product=data, editing=True)
        clean["id"] = product_id
        db.execute(
            "UPDATE products SET name=:name, description=:description, price=:price, "
            "stock=:stock, category=:category, image_url=:image_url WHERE id=:id",
            clean,
        )
        db.commit()
        flash("Product updated.", "success")
        return redirect(url_for("admin_products"))
    return render_template("admin_form.html", product=product, editing=True)


@app.route("/admin/delete/<int:product_id>", methods=["POST"])
@admin_required
def admin_delete(product_id):
    error = delete_product(get_db(), product_id)
    if error:
        flash(error, "error")
    else:
        flash("Product deleted.", "success")
    return redirect(url_for("admin_products"))


@app.route("/admin/orders")
@admin_required
def admin_orders():
    orders = get_db().execute(
        "SELECT o.*, u.name AS customer, u.email FROM orders o "
        "JOIN users u ON u.id = o.user_id ORDER BY o.id DESC"
    ).fetchall()
    return render_template("admin_orders.html", orders=orders, statuses=ORDER_STATUSES)


@app.route("/admin/orders/<int:order_id>/status", methods=["POST"])
@admin_required
def admin_order_status(order_id):
    status = request.form.get("status")
    if status not in ORDER_STATUSES:
        abort(400)
    db = get_db()
    db.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    db.commit()
    flash(f"Order #{order_id} marked as {status}.", "success")
    return redirect(url_for("admin_orders"))


# =====================================================
#  STEP 4 (part 1): USER ACCOUNTS
# =====================================================

def safe_next(url):
    """Only allow redirects to pages inside our own site."""
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return None


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        error = None
        if not name or not email or not password:
            error = "All fields are required."
        elif "@" not in email:
            error = "Please enter a valid email."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."

        db = get_db()
        if not error and db.execute(
            "SELECT 1 FROM users WHERE email = ?", (email,)
        ).fetchone():
            error = "That email is already registered."

        if error:
            flash(error, "error")
            return render_template("register.html", name=name, email=email)

        cur = db.execute(
            "INSERT INTO users (name, email, password_hash) VALUES (?,?,?)",
            (name, email, generate_password_hash(password)),
        )
        db.commit()
        session["user_id"] = cur.lastrowid
        flash("Welcome to ShopEasy!", "success")
        return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = request.args.get("next") or request.form.get("next") or ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Invalid email or password.", "error")
            return render_template("login.html", email=email, next=nxt)

        session.clear()
        session["user_id"] = user["id"]
        flash(f"Welcome back, {user['name']}!", "success")
        return redirect(safe_next(nxt) or url_for("index"))
    return render_template("login.html", next=nxt)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))


# =====================================================
#  STEP 3: SHOPPING CART
# =====================================================

def get_cart(db, user_id):
    items = db.execute(
        "SELECT c.id, c.quantity, p.id AS product_id, p.name, p.price, p.stock, p.image_url "
        "FROM cart_items c JOIN products p ON p.id = c.product_id "
        "WHERE c.user_id = ? ORDER BY c.id",
        (user_id,),
    ).fetchall()
    total = sum(i["price"] * i["quantity"] for i in items)
    return items, total


@app.route("/cart")
@login_required
def cart():
    items, total = get_cart(get_db(), g.user["id"])
    return render_template("cart.html", items=items, total=total)


@app.route("/cart/add/<int:product_id>", methods=["POST"])
@login_required
def cart_add(product_id):
    db = get_db()
    product = db.execute(
        "SELECT * FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if product is None:
        abort(404)

    qty = request.form.get("quantity", 1, type=int)
    if qty < 1:
        qty = 1

    if product["stock"] <= 0:
        flash("Sorry, this product is out of stock.", "error")
        return redirect(request.referrer or url_for("index"))

    existing = db.execute(
        "SELECT * FROM cart_items WHERE user_id = ? AND product_id = ?",
        (g.user["id"], product_id),
    ).fetchone()
    new_qty = qty + (existing["quantity"] if existing else 0)

    if new_qty > product["stock"]:
        new_qty = product["stock"]
        flash(f"Only {product['stock']} available. Quantity adjusted.", "error")
    else:
        flash(f"{product['name']} added to your cart.", "success")

    if existing:
        db.execute("UPDATE cart_items SET quantity = ? WHERE id = ?", (new_qty, existing["id"]))
    else:
        db.execute(
            "INSERT INTO cart_items (user_id, product_id, quantity) VALUES (?,?,?)",
            (g.user["id"], product_id, new_qty),
        )
    db.commit()
    return redirect(request.referrer or url_for("index"))


@app.route("/cart/update/<int:item_id>", methods=["POST"])
@login_required
def cart_update(item_id):
    db = get_db()
    item = db.execute(
        "SELECT c.*, p.stock FROM cart_items c JOIN products p ON p.id = c.product_id "
        "WHERE c.id = ? AND c.user_id = ?",
        (item_id, g.user["id"]),
    ).fetchone()
    if item is None:
        abort(404)

    qty = request.form.get("quantity", 1, type=int)
    if qty <= 0:
        db.execute("DELETE FROM cart_items WHERE id = ?", (item_id,))
    else:
        if qty > item["stock"]:
            qty = item["stock"]
            flash(f"Only {item['stock']} available. Quantity adjusted.", "error")
        db.execute("UPDATE cart_items SET quantity = ? WHERE id = ?", (qty, item_id))
    db.commit()
    return redirect(url_for("cart"))


@app.route("/cart/remove/<int:item_id>", methods=["POST"])
@login_required
def cart_remove(item_id):
    db = get_db()
    db.execute(
        "DELETE FROM cart_items WHERE id = ? AND user_id = ?", (item_id, g.user["id"])
    )
    db.commit()
    flash("Item removed.", "success")
    return redirect(url_for("cart"))


# =====================================================
#  STEP 4 (part 2): CHECKOUT AND ORDERS
# =====================================================

@app.route("/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    db = get_db()
    items, total = get_cart(db, g.user["id"])
    if not items:
        flash("Your cart is empty.", "error")
        return redirect(url_for("cart"))

    form = request.form
    if request.method == "POST":
        name = form.get("name", "").strip()
        phone = form.get("phone", "").strip()
        address = form.get("address", "").strip()
        city = form.get("city", "").strip()
        pin = form.get("pin", "").strip()
        payment = form.get("payment", "Cash on delivery")

        if not all([name, phone, address, city, pin]):
            flash("Please fill in all delivery details.", "error")
            return render_template("checkout.html", items=items, total=total, form=form)

        # make sure everything is still in stock
        for item in items:
            if item["quantity"] > item["stock"]:
                flash(f"Not enough stock for {item['name']} (only {item['stock']} left).", "error")
                return redirect(url_for("cart"))

        full_address = f"{name}, {phone}, {address}, {city} - {pin}"
        try:
            cur = db.execute(
                "INSERT INTO orders (user_id, total, status, address, payment) "
                "VALUES (?,?,?,?,?)",
                (g.user["id"], total, "Placed", full_address, payment),
            )
            order_id = cur.lastrowid
            for item in items:
                db.execute(
                    "INSERT INTO order_items (order_id, product_id, quantity, price) "
                    "VALUES (?,?,?,?)",
                    (order_id, item["product_id"], item["quantity"], item["price"]),
                )
                result = db.execute(
                    "UPDATE products SET stock = stock - ? WHERE id = ? AND stock >= ?",
                    (item["quantity"], item["product_id"], item["quantity"]),
                )
                if result.rowcount == 0:
                    raise ValueError(f"Not enough stock for {item['name']}")
            db.execute("DELETE FROM cart_items WHERE user_id = ?", (g.user["id"],))
            db.commit()
        except ValueError as e:
            db.rollback()
            flash(str(e), "error")
            return redirect(url_for("cart"))

        flash(f"Order #{order_id} placed successfully. Thank you for shopping!", "success")
        return redirect(url_for("orders"))

    return render_template("checkout.html", items=items, total=total, form={"name": g.user["name"]})


@app.route("/orders")
@login_required
def orders():
    db = get_db()
    result = []
    rows = db.execute(
        "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC", (g.user["id"],)
    ).fetchall()
    for order in rows:
        items = db.execute(
            "SELECT oi.quantity, oi.price, p.name FROM order_items oi "
            "JOIN products p ON p.id = oi.product_id WHERE oi.order_id = ?",
            (order["id"],),
        ).fetchall()
        result.append({"order": order, "items": items})
    return render_template("orders.html", orders=result)


# =====================================================
#  ERROR PAGES
# =====================================================

@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, message="You don't have permission to view this page."), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="Page not found."), 404


if __name__ == "__main__":
    app.run(debug=True)