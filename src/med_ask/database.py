"""Application-owned index bookkeeping and question log; no SQL retrieval."""

import json
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb


class Database:
    def __init__(self, url: str):
        self.url = url

    def connect(self):
        return psycopg.connect(self.url, connect_timeout=3)

    def ensure(self):
        with self.connect() as db:
            # Serialize simultaneous first requests in the two app services.
            db.execute("SELECT pg_advisory_xact_lock(7152301)")
            db.execute("""CREATE TABLE IF NOT EXISTS medask_models (
                table_name text PRIMARY KEY, model text NOT NULL,
                dimensions integer NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS medask_books (
                table_name text NOT NULL, book_id text NOT NULL,
                extracted integer NOT NULL, PRIMARY KEY (table_name, book_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS medask_questions (
                id uuid PRIMARY KEY, question text NOT NULL, asker text NOT NULL,
                asked_at timestamptz NOT NULL DEFAULT now(), vector_table text,
                evidence jsonb NOT NULL DEFAULT '[]',
                thumbs text CHECK (thumbs IN ('up', 'down')), comment text,
                feedback_at timestamptz)""")

    def models(self):
        with self.connect() as db:
            return db.execute(
                "SELECT table_name, model, dimensions FROM medask_models ORDER BY model"
            ).fetchall()

    def model(self, table):
        with self.connect() as db:
            row = db.execute(
                "SELECT model, dimensions, to_regclass(%s) IS NOT NULL "
                "FROM medask_models WHERE table_name=%s",
                ("public.data_" + table, table),
            ).fetchone()
        return row

    def register(self, table, model, dimensions):
        with self.connect() as db:
            db.execute(
                "INSERT INTO medask_models VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (table, model, dimensions),
            )
            recorded = db.execute(
                "SELECT model, dimensions FROM medask_models WHERE table_name=%s",
                (table,),
            ).fetchone()
            if recorded != (model, dimensions):
                raise ValueError("Recorded embedding model differs; use a new index")

    def book_count(self, table, book, count):
        with self.connect() as db:
            db.execute(
                "INSERT INTO medask_books VALUES (%s,%s,%s) "
                "ON CONFLICT (table_name,book_id) DO UPDATE "
                "SET extracted=EXCLUDED.extracted",
                (table, book, count),
            )

    def begin_question(self, question, asker):
        identity = str(uuid4())
        with self.connect() as db:
            db.execute(
                "INSERT INTO medask_questions (id,question,asker) VALUES (%s,%s,%s)",
                (identity, question, asker),
            )
        return identity

    def finish_question(self, identity, table, evidence):
        with self.connect() as db:
            db.execute(
                "UPDATE medask_questions SET vector_table=%s,evidence=%s WHERE id=%s",
                (table, Jsonb(evidence), identity),
            )

    def feedback(self, identity, asker, thumbs, comment):
        with self.connect() as db:
            row = db.execute(
                "UPDATE medask_questions SET thumbs=%s,comment=%s,"
                "feedback_at=now() WHERE id=%s AND asker=%s RETURNING id",
                (thumbs, comment, identity, asker),
            ).fetchone()
        return row is not None

    def export(self, directory: Path) -> Path:
        directory = directory.resolve()
        path = directory / f"questions-{uuid4()}.jsonl"
        with self.connect() as db, db.cursor(name="question_export") as cursor:
            cursor.execute(
                "SELECT row_to_json(q) FROM medask_questions q ORDER BY asked_at"
            )
            with path.open("x", encoding="utf-8") as file:
                for (row,) in cursor:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path
