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

            for column in (
                "language text",
                "grades jsonb NOT NULL DEFAULT '[]'",
                "ungraded_count integer NOT NULL DEFAULT 0",
                "answer text",
            ):
                db.execute(
                    "ALTER TABLE medask_questions ADD COLUMN IF NOT EXISTS " + column
                )
            db.execute("""CREATE TABLE IF NOT EXISTS medask_translations (
                passage_id text NOT NULL, language text NOT NULL, text text NOT NULL,
                PRIMARY KEY (passage_id, language))""")

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

    def begin_question(self, question, asker, language="en"):
        identity = str(uuid4())
        with self.connect() as db:
            db.execute(
                "INSERT INTO medask_questions (id,question,asker,language) "
                "VALUES (%s,%s,%s,%s)",
                (identity, question.replace("\x00", ""), asker, language),
            )
        return identity

    def finish_question(self, identity, table, evidence, grades=None, ungraded_count=0):
        with self.connect() as db:
            db.execute(
                "UPDATE medask_questions SET vector_table=%s,evidence=%s,grades=%s,"
                "ungraded_count=%s WHERE id=%s",
                (table, Jsonb(evidence), Jsonb(grades or []), ungraded_count, identity),
            )

    def question(self, identity, asker):
        with self.connect() as db:
            row = db.execute(
                "SELECT question,language,evidence,answer,grades FROM medask_questions "
                "WHERE id=%s AND asker=%s",
                (identity, asker),
            ).fetchone()
        return (
            dict(
                zip(
                    ("question", "language", "evidence", "answer", "grades"),
                    row,
                    strict=True,
                )
            )
            if row
            else None
        )

    def save_answer(self, identity, asker, answer):
        with self.connect() as db:
            db.execute(
                "UPDATE medask_questions SET answer=%s WHERE id=%s AND asker=%s",
                (answer.replace("\x00", ""), identity, asker),
            )

    def translation(self, passage, language):
        with self.connect() as db:
            row = db.execute(
                "SELECT text FROM medask_translations "
                "WHERE passage_id=%s AND language=%s",
                (passage, language),
            ).fetchone()
        return row[0] if row else None

    def save_translation(self, passage, language, text):
        with self.connect() as db:
            db.execute(
                "INSERT INTO medask_translations VALUES (%s,%s,%s) "
                "ON CONFLICT (passage_id,language) DO NOTHING",
                (passage, language, text),
            )

    def feedback(self, identity, asker, thumbs, comment):
        with self.connect() as db:
            row = db.execute(
                "UPDATE medask_questions SET thumbs=%s,comment=%s,"
                "feedback_at=now() WHERE id=%s AND asker=%s RETURNING id",
                (
                    thumbs,
                    comment.replace("\x00", "") if comment is not None else None,
                    identity,
                    asker,
                ),
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
