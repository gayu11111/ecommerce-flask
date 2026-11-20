import sqlite3
from werkzeug.security import generate_password_hash

db = sqlite3.connect("shop.db")
with open("schema.sql") as f:
    db.executescript(f.read())

# Sample products
products = [
    ("Wireless Mouse", "Smooth 2.4GHz wireless mouse", 599, 25, "Electronics"),
    ("Bluetooth Speaker", "Portable speaker, 10h battery", 1499, 15, "Electronics"),
    ("Notebook Pack", "Pack of 5 ruled notebooks", 249, 50, "Stationery"),
    ("Gel Pen Set", "Set of 10 colour gel pens", 199, 4, "Stationery"),
    ("Cotton T-Shirt", "Soft cotton, round neck", 399, 40, "Clothing"),
    ("Backpack", "Water-resistant 25L backpack", 999, 0, "Accessories"),
]

for name, desc, price, stock, cat in products:
    img = f"https://placehold.co/400x300/FFE8D6/7C2D12?text={name.replace(' ', '+')}"
    db.execute(
        "INSERT INTO products (name, description, price, stock, category, image_url) "
        "VALUES (?,?,?,?,?,?)",
        (name, desc, price, stock, cat, img),
    )

# Admin account
db.execute(
    "INSERT INTO users (name, email, password_hash, is_admin) VALUES (?,?,?,1)",
    ("Admin", "admin@shop.com", generate_password_hash("admin123")),
)

db.commit()
db.close()
print("Database ready!")
print("6 sample products added.")
print("Admin login -> email: admin@shop.com  password: admin123")