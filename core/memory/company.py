from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class CompanyMemoryDoc(BaseModel):
    """A document or chunk stored in company memory."""

    doc_id: str
    category: str  # "procedure", "system", "policy", "contact", "connector", "fact"
    title: str
    content: str
    source_file: str | None = None
    rank: float = Field(default=0.0)


class CompanyMemoryIndex:
    """
    SQLite FTS5 / BM25 indexed company memory retriever.
    Indexes systems, standard operating procedures (SOPs), policies, contacts,
    and promoted memory facts for context-aware task understanding and planning.
    """

    def __init__(self, company_dir: Path | str = "company", db_path: str = ":memory:") -> None:
        self.company_dir = Path(company_dir)
        self.db_path = db_path
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_db()
        self.reindex()

    def _init_db(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS company_memory USING fts5(
                    doc_id UNINDEXED,
                    category,
                    title,
                    content,
                    source_file UNINDEXED,
                    tokenize='porter unicode61'
                );
                """
            )

    def reindex(self, additional_facts: list[dict[str, Any]] | None = None) -> None:
        """Scan company directory and index all SOPs, systems, policies, contacts, and connectors."""
        with self._conn:
            self._conn.execute("DELETE FROM company_memory")

        if not self.company_dir.exists():
            logger.warning(f"Company directory '{self.company_dir}' does not exist; memory index is empty.")
            return

        docs_to_insert: list[tuple[str, str, str, str, str]] = []

        # 1. Index Systems (systems.yaml) & Credentials Vault
        systems_path = self.company_dir / "systems.yaml"
        vault_path = self.company_dir / "vault.yaml"
        from core.memory.vault import CredentialVault
        vault = CredentialVault(vault_path)

        if systems_path.exists():
            try:
                import yaml

                raw = yaml.safe_load(systems_path.read_text(encoding="utf-8")) or {}
                systems = raw.get("systems", {})
                for sys_key, sys_info in systems.items():
                    name = sys_info.get("name", sys_key)
                    desc = sys_info.get("description", "")
                    caps = ", ".join(sys_info.get("capabilities", []))
                    url = sys_info.get("url", "")
                    login_info = sys_info.get("login")
                    login_str = ""
                    if login_info:
                        login_url = login_info.get("login_url", "")
                        cred_key = login_info.get("default_credentials", "")
                        creds_detail = ""
                        if cred_key:
                            creds = vault.get(cred_key)
                            if creds:
                                u_name = creds.get("username") or creds.get("email")
                                p_word = creds.get("password")
                                creds_detail = f" (username: '{u_name}', password: '{p_word}')"
                        login_str = f"\nLogin URL: {login_url}\nCredentials: {cred_key}{creds_detail}"
                    content = f"System: {name}\nURL: {url}\nDescription: {desc}\nCapabilities: {caps}{login_str}"
                    docs_to_insert.append(
                        (f"system:{sys_key}", "system", name, content, str(systems_path))
                    )
            except Exception as e:
                logger.error(f"Error parsing systems.yaml: {e}")

        # 2. Index Policies (policies.yaml)
        policies_path = self.company_dir / "policies.yaml"
        if policies_path.exists():
            try:
                import yaml

                raw = yaml.safe_load(policies_path.read_text(encoding="utf-8")) or {}
                rules = raw.get("rules", [])
                for rule in rules:
                    r_id = rule.get("id", "rule")
                    desc = rule.get("description", "")
                    cond = rule.get("condition", "")
                    eff = rule.get("effect", "")
                    role = rule.get("approver_role", "")
                    content = (
                        f"Policy Rule: {r_id}\nDescription: {desc}\nCondition: {cond}\n"
                        f"Effect: {eff}\nApprover Role: {role}"
                    )
                    docs_to_insert.append(
                        (f"policy:{r_id}", "policy", f"Policy: {r_id}", content, str(policies_path))
                    )
            except Exception as e:
                logger.error(f"Error parsing policies.yaml: {e}")

        # 3. Index Contacts (contacts.yaml)
        contacts_path = self.company_dir / "contacts.yaml"
        if contacts_path.exists():
            try:
                import yaml

                raw = yaml.safe_load(contacts_path.read_text(encoding="utf-8")) or {}
                contacts = raw.get("contacts", {})
                for c_key, c_info in contacts.items():
                    name = c_info.get("name", c_key)
                    role = c_info.get("role", "")
                    email = c_info.get("email", "")
                    scope = c_info.get("approval_scope", "")
                    content = f"Contact: {name}\nRole: {role}\nEmail: {email}\nApproval Scope: {scope}"
                    docs_to_insert.append(
                        (f"contact:{c_key}", "contact", f"Contact: {name}", content, str(contacts_path))
                    )
            except Exception as e:
                logger.error(f"Error parsing contacts.yaml: {e}")

        # 4. Index Procedures (procedures/*.md)
        proc_dir = self.company_dir / "procedures"
        if proc_dir.exists():
            for p in proc_dir.glob("*.md"):
                try:
                    text = p.read_text(encoding="utf-8")
                    title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
                    title = title_match.group(1).strip() if title_match else p.stem.replace("_", " ").title()
                    docs_to_insert.append(
                        (f"procedure:{p.stem}", "procedure", title, text, str(p))
                    )
                except Exception as e:
                    logger.error(f"Error indexing procedure {p}: {e}")

        # 5. Index Connectors (connectors/*.yaml)
        conn_dir = self.company_dir / "connectors"
        if conn_dir.exists():
            for c in conn_dir.glob("*.yaml"):
                try:
                    import yaml

                    raw = yaml.safe_load(c.read_text(encoding="utf-8")) or {}
                    name = raw.get("name", c.stem)
                    desc = raw.get("description", "")
                    content = f"Connector: {name}\nDescription: {desc}\nDetails:\n{c.read_text(encoding='utf-8')}"
                    docs_to_insert.append(
                        (f"connector:{c.stem}", "connector", f"Connector: {name}", content, str(c))
                    )
                except Exception as e:
                    logger.error(f"Error indexing connector {c}: {e}")

        # 6. Index Promoted Facts
        if additional_facts:
            for fact in additional_facts:
                f_id = f"fact:{fact.get('id', fact.get('key', 'unknown'))}"
                title = f"Verified Fact: {fact.get('key', 'Unknown')}"
                content = f"Category: {fact.get('category')}\nKey: {fact.get('key')}\nValue: {fact.get('value')}"
                docs_to_insert.append((f_id, "fact", title, content, "promoted_memory"))

        with self._conn:
            self._conn.executemany(
                "INSERT INTO company_memory (doc_id, category, title, content, source_file) VALUES (?, ?, ?, ?, ?)",
                docs_to_insert,
            )

        logger.info(f"Indexed {len(docs_to_insert)} company memory documents into SQLite FTS5.")

    def _clean_query(self, query: str) -> str:
        """Sanitize query string into safe FTS5 boolean query."""
        # Remove FTS punctuation and operators
        words = re.findall(r"\b[A-Za-z0-9_-]{2,}\b", query)
        # Filter common stopwords
        stopwords = {
            "the", "and", "or", "to", "in", "of", "for", "on", "with", "at", "by", "from",
            "is", "it", "an", "as", "be", "this", "that", "all", "are", "was", "were", "me",
            "my", "our", "you", "your", "can", "will", "please", "into", "done", "once"
        }
        filtered = [w for w in words if w.lower() not in stopwords]
        if not filtered:
            filtered = words
        if not filtered:
            return ""
        # Format as OR query for max recall with BM25 ranking
        return " OR ".join(f'"{w}"' for w in filtered)

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        categories: list[str] | None = None,
    ) -> list[CompanyMemoryDoc]:
        """
        Retrieve relevant company memory documents using SQLite FTS5 BM25 ranking.
        Falls back to returning core procedure/system docs if search returns empty.
        """
        fts_query = self._clean_query(query)
        docs: list[CompanyMemoryDoc] = []

        if fts_query:
            try:
                sql = (
                    "SELECT doc_id, category, title, content, source_file, bm25(company_memory) as rank "
                    "FROM company_memory WHERE company_memory MATCH ? "
                )
                params: list[Any] = [fts_query]
                if categories:
                    placeholders = ",".join("?" for _ in categories)
                    sql += f"AND category IN ({placeholders}) "
                    params.extend(categories)
                sql += "ORDER BY rank LIMIT ?"
                params.append(top_k)

                cur = self._conn.execute(sql, params)
                for row in cur.fetchall():
                    docs.append(
                        CompanyMemoryDoc(
                            doc_id=row["doc_id"],
                            category=row["category"],
                            title=row["title"],
                            content=row["content"],
                            source_file=row["source_file"],
                            rank=float(row["rank"]),
                        )
                    )
            except Exception as e:
                logger.warning(f"FTS5 query failed for '{fts_query}': {e}")

        # Fallback if no docs retrieved
        if not docs:
            cur = self._conn.execute("SELECT doc_id, category, title, content, source_file FROM company_memory LIMIT ?", (top_k,))
            for row in cur.fetchall():
                docs.append(
                    CompanyMemoryDoc(
                        doc_id=row["doc_id"],
                        category=row["category"],
                        title=row["title"],
                        content=row["content"],
                        source_file=row["source_file"],
                        rank=0.0,
                    )
                )

        return docs

    def format_context(self, docs: list[CompanyMemoryDoc]) -> str:
        """Format retrieved company memory docs into a clear markdown prompt block."""
        if not docs:
            return "No specific company memory retrieved."

        sections: list[str] = ["## Retrieved Company Memory & Procedures:"]
        for d in docs:
            src = f" (Source: {d.source_file})" if d.source_file else ""
            sections.append(f"### [{d.category.upper()}] {d.title}{src}\n{d.content}\n")

        return "\n".join(sections)
