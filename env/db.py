from __future__ import annotations

import os
import sqlite3
from typing import Any

DB_PATH = os.getenv("COMPANY_DB_PATH", os.path.join(os.path.dirname(__file__), "company.db"))


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db(db_path: str = DB_PATH) -> None:
    """Initialize database tables with schema."""
    conn = get_connection(db_path)
    cursor = conn.cursor()

    cursor.executescript(
        """
        CREATE TABLE IF NOT EXISTS vendors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            code TEXT UNIQUE NOT NULL,
            contact_email TEXT NOT NULL,
            tax_id TEXT,
            status TEXT DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT UNIQUE NOT NULL,
            vendor_id INTEGER NOT NULL,
            vendor_name TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            invoice_date TEXT NOT NULL,
            due_date TEXT NOT NULL,
            status TEXT DEFAULT 'unpaid',
            pdf_filename TEXT NOT NULL,
            is_on_portal BOOLEAN DEFAULT 1,
            is_in_mailbox BOOLEAN DEFAULT 0,
            is_in_fileshare BOOLEAN DEFAULT 1,
            notes TEXT,
            FOREIGN KEY (vendor_id) REFERENCES vendors (id)
        );

        CREATE TABLE IF NOT EXISTS erp_bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_name TEXT NOT NULL,
            invoice_number TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            due_date TEXT NOT NULL,
            attachment_filename TEXT,
            status TEXT DEFAULT 'entered',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(vendor_name, invoice_number)
        );

        CREATE TABLE IF NOT EXISTS emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            attachment_filename TEXT,
            date TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_number TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            assignee TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            priority TEXT NOT NULL DEFAULT 'medium',
            created_at TEXT NOT NULL,
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            system TEXT NOT NULL,
            role TEXT NOT NULL
        );
        """
    )

    # Insert default users if not present
    cursor.execute(
        """
        INSERT OR IGNORE INTO users (username, password, system, role) VALUES
        ('admin', 'erp_pass123', 'erp', 'finance_operator'),
        ('supplier@acme.com', 'portal_pass123', 'portal', 'vendor'),
        ('operator', 'tickets_pass123', 'tickets', 'helpdesk')
        """
    )

    conn.commit()
    conn.close()


def reset_db(db_path: str = DB_PATH) -> None:
    """Clear all data and recreate schema."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.executescript(
        """
        DROP TABLE IF EXISTS erp_bills;
        DROP TABLE IF EXISTS invoices;
        DROP TABLE IF EXISTS vendors;
        DROP TABLE IF EXISTS emails;
        DROP TABLE IF EXISTS tickets;
        DROP TABLE IF EXISTS users;
        DELETE FROM sqlite_sequence;
        """
    )
    conn.commit()
    conn.close()
    init_db(db_path)


def query_all(
    query: str, params: tuple[Any, ...] = (), db_path: str = DB_PATH
) -> list[dict[str, Any]]:
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def query_one(
    query: str, params: tuple[Any, ...] = (), db_path: str = DB_PATH
) -> dict[str, Any] | None:
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute(query, params)
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def execute_write(query: str, params: tuple[Any, ...] = (), db_path: str = DB_PATH) -> int:
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute(query, params)
    conn.commit()
    last_id = cursor.lastrowid or 0
    conn.close()
    return last_id
