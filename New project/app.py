from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request

from flask import Flask, jsonify, render_template, request as flask_request


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "instance" / "app.db"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"


def load_dotenv() -> None:
    dotenv_path = BASE_DIR / ".env"
    if not dotenv_path.exists():
        return

    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()
DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False


def get_db() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def query_all(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def query_one(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute(query, params).fetchone()
    return dict(row) if row else None


def execute(query: str, params: tuple[Any, ...] = ()) -> int:
    with get_db() as db:
        cursor = db.execute(query, params)
        db.commit()
        return cursor.lastrowid


def init_db() -> None:
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS centers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                class_size TEXT NOT NULL DEFAULT '',
                available_equipment TEXT NOT NULL DEFAULT '',
                permanent_feedback TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS center_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                center_id INTEGER NOT NULL,
                instruction TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(center_id) REFERENCES centers(id)
            );

            CREATE TABLE IF NOT EXISTS generations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                center_id INTEGER NOT NULL,
                source_workout TEXT NOT NULL,
                adapted_workout TEXT NOT NULL,
                briefing TEXT NOT NULL DEFAULT '',
                center_snapshot TEXT NOT NULL,
                feedback_snapshot TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(center_id) REFERENCES centers(id)
            );
            """
        )
        db.commit()

    migrate_center_schema()
    seed_centers()
    normalize_default_centers()
    merge_feedback_history_into_permanent_feedback()


def migrate_center_schema() -> None:
    with get_db() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(centers)").fetchall()}
        migrations = {
            "class_size": "ALTER TABLE centers ADD COLUMN class_size TEXT NOT NULL DEFAULT ''",
            "available_equipment": "ALTER TABLE centers ADD COLUMN available_equipment TEXT NOT NULL DEFAULT ''",
            "restrictions_priorities": "ALTER TABLE centers ADD COLUMN restrictions_priorities TEXT NOT NULL DEFAULT ''",
            "permanent_feedback": "ALTER TABLE centers ADD COLUMN permanent_feedback TEXT NOT NULL DEFAULT ''",
        }
        for column, statement in migrations.items():
            if column not in columns:
                db.execute(statement)

        generation_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(generations)").fetchall()
        }
        if "briefing" not in generation_columns:
            db.execute("ALTER TABLE generations ADD COLUMN briefing TEXT NOT NULL DEFAULT ''")

        legacy_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(centers)").fetchall()
        }
        if "audience" in legacy_columns:
            db.execute(
                """
                UPDATE centers
                SET class_size = audience
                WHERE class_size = '' AND audience != ''
                """
            )
        if "facility_notes" in legacy_columns:
            db.execute(
                """
                UPDATE centers
                SET available_equipment = facility_notes
                WHERE available_equipment = '' AND facility_notes != ''
                """
            )
        if {"movement_constraints", "coaching_style", "permanent_instructions"}.issubset(legacy_columns):
            db.execute(
                """
                UPDATE centers
                SET restrictions_priorities =
                    TRIM(
                        COALESCE(movement_constraints, '') || CHAR(10) ||
                        COALESCE(coaching_style, '') || CHAR(10) ||
                        COALESCE(permanent_instructions, '')
                    )
                WHERE restrictions_priorities = ''
                  AND (
                    movement_constraints != ''
                    OR coaching_style != ''
                    OR permanent_instructions != ''
                  )
                """
            )
        if "restrictions_priorities" in legacy_columns:
            db.execute(
                """
                UPDATE centers
                SET permanent_feedback = restrictions_priorities
                WHERE permanent_feedback = '' AND restrictions_priorities != ''
                """
            )
        db.commit()


def seed_centers() -> None:
    existing = query_one("SELECT COUNT(*) AS count FROM centers")
    if existing and existing["count"] > 0:
        return

    now = utc_now()
    defaults = [
        {
            "slug": "centro-1",
            "name": "Centro 1",
            "class_size": "12-16 personas por clase.",
            "available_equipment": "Material estandar de box y espacio suficiente para rotaciones simples.",
            "permanent_feedback": "Evitar complejidad tecnica innecesaria si no aporta al estimulo. Mantener bloques bien separados y explicar escalados solo cuando sean necesarios.",
        },
        {
            "slug": "centro-2",
            "name": "Centro 2",
            "class_size": "18-24 personas por clase.",
            "available_equipment": "Menos material repetido y grupos grandes en horas punta.",
            "permanent_feedback": "Priorizar seguridad, flujo de clase y movimientos faciles de ensenar. Cuando haya cuello de botella de material, proponer alternativas equivalentes sin cambiar el estimulo.",
        },
        {
            "slug": "centro-3",
            "name": "Centro 3",
            "class_size": "10-14 personas por clase.",
            "available_equipment": "Buen acceso a material y posibilidad de trabajar por parejas o heats.",
            "permanent_feedback": "Se puede subir el nivel tecnico si el estimulo se mantiene intacto. Si una variante mejora el flujo sin alterar el objetivo fisiologico, usala.",
        },
    ]

    with get_db() as db:
        for center in defaults:
            db.execute(
                """
                INSERT INTO centers (
                    slug, name, class_size, available_equipment,
                    permanent_feedback, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    center["slug"],
                    center["name"],
                    center["class_size"],
                    center["available_equipment"],
                    center["permanent_feedback"],
                    now,
                    now,
                ),
            )
        db.commit()


def normalize_default_centers() -> None:
    defaults = {
        "centro-1": {
            "legacy_class_size": "Grupo general con nivel mixto y buena tolerancia de volumen.",
            "class_size": "12-16 personas por clase.",
            "available_equipment": "Material estandar de box y espacio suficiente para rotaciones simples.",
            "permanent_feedback": "Evitar complejidad tecnica innecesaria si no aporta al estimulo. Mantener bloques bien separados y explicar escalados solo cuando sean necesarios.",
        },
        "centro-2": {
            "legacy_class_size": "Perfil más principiante y necesidad de opciones de accesibilidad frecuentes.",
            "class_size": "18-24 personas por clase.",
            "available_equipment": "Menos material repetido y grupos grandes en horas punta.",
            "permanent_feedback": "Priorizar seguridad, flujo de clase y movimientos faciles de ensenar. Cuando haya cuello de botella de material, proponer alternativas equivalentes sin cambiar el estimulo.",
        },
        "centro-3": {
            "legacy_class_size": "Alumnado con más experiencia y mejor tolerancia a complejidad moderada.",
            "class_size": "10-14 personas por clase.",
            "available_equipment": "Buen acceso a material y posibilidad de trabajar por parejas o heats.",
            "permanent_feedback": "Se puede subir el nivel tecnico si el estimulo se mantiene intacto. Si una variante mejora el flujo sin alterar el objetivo fisiologico, usala.",
        },
    }

    with get_db() as db:
        for slug, values in defaults.items():
            db.execute(
                """
                UPDATE centers
                SET class_size = ?, available_equipment = ?,
                    permanent_feedback = ?, updated_at = ?
                WHERE slug = ?
                  AND (class_size = ? OR class_size = '')
                """,
                (
                    values["class_size"],
                    values["available_equipment"],
                    values["permanent_feedback"],
                    utc_now(),
                    slug,
                    values["legacy_class_size"],
                ),
            )
        db.commit()


def merge_feedback_history_into_permanent_feedback() -> None:
    centers = query_all("SELECT id, permanent_feedback FROM centers ORDER BY id ASC")
    with get_db() as db:
        for center in centers:
            rows = db.execute(
                """
                SELECT instruction
                FROM center_feedback
                WHERE center_id = ?
                ORDER BY id ASC
                """,
                (center["id"],),
            ).fetchall()
            memory_lines = [
                line.strip()
                for line in center["permanent_feedback"].splitlines()
                if line.strip()
            ]
            memory_text = "\n".join(memory_lines)
            for row in rows:
                instruction = row["instruction"].strip()
                if instruction and instruction not in memory_text:
                    memory_lines.append(instruction)
                    memory_text = "\n".join(memory_lines)

            merged_memory = "\n".join(memory_lines)
            if merged_memory != center["permanent_feedback"]:
                db.execute(
                    """
                    UPDATE centers
                    SET permanent_feedback = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (merged_memory, utc_now(), center["id"]),
                )
        db.commit()


def get_centers_with_context() -> list[dict[str, Any]]:
    centers = query_all(
        """
        SELECT *
        FROM centers
        ORDER BY id ASC
        """
    )
    for center in centers:
        center["feedback"] = query_all(
            """
            SELECT id, instruction, created_at
            FROM center_feedback
            WHERE center_id = ?
            ORDER BY id DESC
            """,
            (center["id"],),
        )
        latest_generation = query_one(
            """
            SELECT id, adapted_workout, briefing, created_at, model
            FROM generations
            WHERE center_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (center["id"],),
        )
        if latest_generation:
            latest_generation["full_output"] = (
                f"{latest_generation['adapted_workout'].strip()}\n\nBREAFING\n\n"
                f"{latest_generation['briefing'].strip()}"
            ).strip()
        center["latest_generation"] = latest_generation
    return centers


def build_center_prompt(
    center: dict[str, Any],
    workout_text: str,
    temporary_material_block: str = "",
) -> str:
    return f"""
Eres un programador experto en adaptar entrenamientos para centros distintos.

Objetivo 1: entrenamiento adaptado
- Adaptar el entrenamiento original al centro indicado.
- Tu mision es mantenerte lo mas fiel posible al entrenamiento original.
- No cambies nada que no sea estrictamente necesario para adaptar a la realidad del centro y mantener el mismo estimulo.
- Mantener exactamente el mismo estimulo principal del entrenamiento original.
- Si hace falta cambiar formato, volumen, carga, progresion o movimientos, hazlo solo para conservar ese mismo estimulo.
- El calentamiento y la movilidad deben permanecer siempre iguales al original, sin cambios de texto, formato, orden, ejercicios, volumen ni tiempos.
- No inventes explicaciones de por qué cambiaste cosas.
- No uses markdown, tablas, ni encabezados tipo "explicacion" o "notas del modelo".
- Respeta la estructura original siempre que sea posible.
- Entrega el entrenamiento adaptado en 3 bloques fijos y en este orden:
  1. CALENTAMIENTO, MOVILIDAD Y ACTIVACION
  2. ENTRENAMIENTO
  3. ADAPTACIONES Y NOTAS
- En "ADAPTACIONES Y NOTAS" incluye escalados, opciones por material/espacio y notas de ejecucion para el coach, sin romper el estimulo original.

Objetivo 2: briefing de clase
- Genera un briefing especifico para el entrenamiento adaptado.
- Debe durar maximo 3 minutos.
- Debe estar escrito en primera persona del coach hablando a la clase.
- Debe ser directo, cercano y sin frases motivacionales vacias.
- Nunca uses palabras negativas como lesion, dolor o molestia.
- El socio tipo tiene 35-50 anos, trabaja, tiene familia, no es deportista de base y busca sentirse mejor y tener mas energia.
- El briefing debe tener estos 5 bloques fijos, en este orden:
  1. Bienvenida real (20 seg): saludo cercano mirando a los socios, mencionar si hay alguien nuevo.
  2. Que y para que (40 seg): explicar el objetivo del entrenamiento del dia. No describas el WOD ejercicio por ejercicio; explica el proposito, que capacidad trabajamos y por que hoy.
  3. Una sola clave tecnica (40 seg): el punto tecnico mas importante del dia. Solo uno.
  4. Por que importa (30 seg): conexion con la vida real cotidiana del socio, como maletas, ninos, escaleras o postura. Nunca ejemplos de competicion o rendimiento deportivo.
  5. Cierre y arranque (30 seg): preguntar si alguien necesita adaptar algo antes de empezar, tono positivo y arrancar.
- En el texto del briefing, separa los 5 bloques con estos titulos exactos: "1. Bienvenida real", "2. Que y para que", "3. Clave tecnica", "4. Por que importa", "5. Cierre y arranque".
- Cada bloque del briefing debe ir separado por una linea en blanco.
- No escribas el briefing como un unico parrafo.

Datos del centro:
Nombre: {center['name']}
Personas por clase: {center['class_size']}
Material disponible: {center['available_equipment']}
Memoria permanente del centro: {center['permanent_feedback']}
Bloqueo puntual de material para esta generacion: {temporary_material_block or 'Sin bloqueos puntuales.'}

Regla critica:
- Aplica criterio de minima intervencion: conserva estructura, orden, bloques, tiempos y volumen del original siempre que sea viable.
- Copia calentamiento y movilidad exactamente igual que en el entrenamiento original.
- Si hay bloqueo puntual de material en esta generacion, tratalo como restriccion obligatoria temporal con prioridad sobre el material habitual del centro.
- La memoria permanente del centro es obligatoria: si indica que no hay material, espacio o capacidad para un movimiento, no uses ese movimiento en la adaptacion.
- Cuando sustituyas algo por una restriccion de la memoria permanente, conserva el mismo estimulo fisiologico, mecanico, volumen relativo, intensidad y ritmo de trabajo.
- El estimulo del entrenamiento debe ser equivalente al original.
- Si sustituyes un movimiento, la nueva opcion debe perseguir la misma demanda fisiologica, mecanica y de ritmo de trabajo.
- No simplifiques de mas si eso altera el objetivo.

Entrenamiento original:
\"\"\"
{workout_text.strip()}
\"\"\"

Formato de respuesta obligatorio:
- Devuelve exclusivamente un JSON valido, sin markdown y sin texto antes o despues.
- Usa exactamente estas claves: "adapted_workout" y "briefing".
- "adapted_workout" debe ser texto plano listo para copiar y pegar y debe incluir exactamente esos 3 bloques con esos titulos.
- "briefing" debe ser texto plano listo para leer por el coach.
""".strip()


def parse_generation_response(raw_response: str) -> dict[str, str]:
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Claude no devolvio el JSON esperado con entrenamiento y briefing.") from exc

    adapted_workout = normalize_adapted_workout(str(data.get("adapted_workout", "")).strip())
    briefing = normalize_briefing(str(data.get("briefing", "")).strip())
    if not adapted_workout or not briefing:
        raise RuntimeError("Claude devolvio una respuesta incompleta: falta entrenamiento o briefing.")
    return {"adapted_workout": adapted_workout, "briefing": briefing}


def compact_blank_lines(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return re.sub(r"\n{3,}", "\n\n", normalized)


def normalize_adapted_workout(raw_text: str) -> str:
    cleaned = compact_blank_lines(raw_text)
    headings = [
        "CALENTAMIENTO, MOVILIDAD Y ACTIVACION",
        "ENTRENAMIENTO",
        "ADAPTACIONES Y NOTAS",
    ]
    heading_pattern = re.compile(
        r"(?im)^(CALENTAMIENTO,\s*MOVILIDAD\s*Y\s*ACTIVACION|ENTRENAMIENTO|ADAPTACIONES\s*Y\s*NOTAS)\s*$"
    )
    matches = list(heading_pattern.finditer(cleaned))

    if not matches:
        return (
            "CALENTAMIENTO, MOVILIDAD Y ACTIVACION\n\n"
            "Sin cambios respecto al original.\n\n"
            "ENTRENAMIENTO\n\n"
            f"{cleaned}\n\n"
            "ADAPTACIONES Y NOTAS\n\n"
            "Sin notas adicionales."
        ).strip()

    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned)
        heading = match.group(1).upper()
        content = compact_blank_lines(cleaned[start:end]).strip()
        sections[heading] = content

    ordered_sections = []
    for heading in headings:
        content = sections.get(heading, "Sin notas adicionales.")
        ordered_sections.append(f"{heading}\n\n{content}")
    return "\n\n".join(ordered_sections).strip()


def normalize_briefing(raw_text: str) -> str:
    cleaned = compact_blank_lines(raw_text)
    canonical = {
        "bienvenida real": "1. Bienvenida real",
        "que y para que": "2. Que y para que",
        "clave tecnica": "3. Clave tecnica",
        "por que importa": "4. Por que importa",
        "cierre y arranque": "5. Cierre y arranque",
    }
    heading_pattern = re.compile(
        r"(?im)^(?:\d\.\s*)?(Bienvenida real|Que y para que|Clave tecnica|Por que importa|Cierre y arranque)\s*$"
    )
    matches = list(heading_pattern.finditer(cleaned))
    if not matches:
        return cleaned

    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned)
        key = match.group(1).lower()
        title = canonical[key]
        content = compact_blank_lines(cleaned[start:end]).strip()
        sections[title] = content

    ordered_titles = [
        "1. Bienvenida real",
        "2. Que y para que",
        "3. Clave tecnica",
        "4. Por que importa",
        "5. Cierre y arranque",
    ]
    blocks = []
    for title in ordered_titles:
        text = sections.get(title, "")
        if text:
            blocks.append(f"{title}\n\n{text}")
    return "\n\n".join(blocks).strip()


def format_full_output(adapted_workout: str, briefing: str) -> str:
    return f"{adapted_workout.strip()}\n\nBREAFING\n{briefing.strip()}".strip()


def anthropic_messages_api(prompt: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Falta la variable de entorno ANTHROPIC_API_KEY.")

    payload = json.dumps(
        {
            "model": DEFAULT_MODEL,
            "max_tokens": 1800,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")

    http_request = request.Request(
        ANTHROPIC_API_URL,
        data=payload,
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with request.urlopen(http_request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Claude devolvio un error HTTP {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError("No se pudo conectar con la API de Claude.") from exc

    content_blocks = data.get("content", [])
    text_parts = [block.get("text", "") for block in content_blocks if block.get("type") == "text"]
    result = "\n".join(part.strip() for part in text_parts if part.strip()).strip()
    if not result:
        raise RuntimeError("Claude no devolvio contenido de texto.")
    return result


def save_generation(
    center: dict[str, Any],
    source_workout: str,
    adapted_workout: str,
    briefing: str,
    temporary_material_block: str,
    prompt: str,
) -> dict[str, Any]:
    feedback_snapshot = json.dumps(center["feedback"], ensure_ascii=False)
    center_snapshot = json.dumps(
        {
            "name": center["name"],
            "class_size": center["class_size"],
            "available_equipment": center["available_equipment"],
            "permanent_feedback": center["permanent_feedback"],
            "temporary_material_block": temporary_material_block,
            "prompt": prompt,
        },
        ensure_ascii=False,
    )
    created_at = utc_now()
    generation_id = execute(
        """
        INSERT INTO generations (
            center_id, source_workout, adapted_workout, briefing, center_snapshot,
            feedback_snapshot, model, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            center["id"],
            source_workout,
            adapted_workout,
            briefing,
            center_snapshot,
            feedback_snapshot,
            DEFAULT_MODEL,
            created_at,
        ),
    )
    return {
        "id": generation_id,
        "adapted_workout": adapted_workout,
        "briefing": briefing,
        "full_output": format_full_output(adapted_workout, briefing),
        "created_at": created_at,
        "model": DEFAULT_MODEL,
    }


def generate_for_center(
    center_id: int,
    workout_text: str,
    temporary_material_block: str = "",
) -> dict[str, Any]:
    center = get_centers_with_context()
    selected_center = next((item for item in center if item["id"] == center_id), None)
    if not selected_center:
        raise ValueError("Centro no encontrado.")

    prompt = build_center_prompt(selected_center, workout_text, temporary_material_block)
    parsed_response = parse_generation_response(anthropic_messages_api(prompt))
    generation = save_generation(
        selected_center,
        workout_text,
        parsed_response["adapted_workout"],
        parsed_response["briefing"],
        temporary_material_block,
        prompt,
    )
    return {
        "center_id": selected_center["id"],
        "center_name": selected_center["name"],
        "adapted_workout": parsed_response["adapted_workout"],
        "briefing": parsed_response["briefing"],
        "full_output": format_full_output(
            parsed_response["adapted_workout"],
            parsed_response["briefing"],
        ),
        "generation": generation,
    }


@app.route("/")
def index():
    return render_template("index.html", centers=get_centers_with_context(), model=DEFAULT_MODEL)


@app.route("/api/bootstrap")
def bootstrap():
    return jsonify({"centers": get_centers_with_context(), "model": DEFAULT_MODEL})


@app.route("/api/centers/<int:center_id>", methods=["POST"])
def update_center(center_id: int):
    payload = flask_request.get_json(force=True)
    current = query_one("SELECT id FROM centers WHERE id = ?", (center_id,))
    if not current:
        return jsonify({"error": "Centro no encontrado."}), 404

    execute(
        """
        UPDATE centers
        SET name = ?, class_size = ?, available_equipment = ?,
            permanent_feedback = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            payload.get("name", "").strip(),
            payload.get("class_size", "").strip(),
            payload.get("available_equipment", "").strip(),
            payload.get("permanent_feedback", "").strip(),
            utc_now(),
            center_id,
        ),
    )

    updated = next(item for item in get_centers_with_context() if item["id"] == center_id)
    return jsonify({"center": updated, "message": "Centro actualizado."})


@app.route("/api/generate", methods=["POST"])
def generate_all():
    payload = flask_request.get_json(force=True)
    workout_text = payload.get("workout_text", "").strip()
    requested_center_ids = payload.get("center_ids")
    temporary_material_blocks = payload.get("temporary_material_blocks") or {}
    if not workout_text:
        return jsonify({"error": "Pega un entrenamiento antes de generar."}), 400

    centers = get_centers_with_context()
    if requested_center_ids is None:
        selected_centers = centers
    else:
        try:
            selected_ids = {int(center_id) for center_id in requested_center_ids}
        except (TypeError, ValueError):
            return jsonify({"error": "La seleccion de centros no es valida."}), 400
        selected_centers = [center for center in centers if center["id"] in selected_ids]

    if not selected_centers:
        return jsonify({"error": "Elige al menos un centro para generar."}), 400

    results = []
    try:
        for center in selected_centers:
            block = str(
                temporary_material_blocks.get(str(center["id"]))
                or temporary_material_blocks.get(center["id"])
                or ""
            ).strip()
            results.append(generate_for_center(center["id"], workout_text, block))
    except RuntimeError as exc:
        return jsonify({"error": str(exc), "results": results}), 502
    return jsonify({"results": results})


@app.route("/api/centers/<int:center_id>/feedback", methods=["POST"])
def add_feedback(center_id: int):
    payload = flask_request.get_json(force=True)
    instruction = payload.get("instruction", "").strip()
    workout_text = payload.get("workout_text", "").strip()
    regenerate = bool(payload.get("regenerate"))
    temporary_material_block = payload.get("temporary_material_block", "").strip()

    center_exists = query_one("SELECT id FROM centers WHERE id = ?", (center_id,))
    if not center_exists:
        return jsonify({"error": "Centro no encontrado."}), 404
    if not instruction:
        return jsonify({"error": "Escribe un comentario o instruccion antes de guardar."}), 400

    execute(
        """
        UPDATE centers
        SET permanent_feedback = ?, updated_at = ?
        WHERE id = ?
        """,
        (instruction, utc_now(), center_id),
    )

    feedback_id = execute(
        """
        INSERT INTO center_feedback (center_id, instruction, created_at)
        VALUES (?, ?, ?)
        """,
        (center_id, instruction, utc_now()),
    )

    response: dict[str, Any] = {
        "message": "Feedback guardado para futuras generaciones.",
        "feedback": query_one(
            "SELECT id, instruction, created_at FROM center_feedback WHERE id = ?",
            (feedback_id,),
        ),
    }

    if regenerate:
        if not workout_text:
            return jsonify({"error": "Hace falta el entrenamiento original para regenerar."}), 400
        try:
            response["result"] = generate_for_center(
                center_id,
                workout_text,
                temporary_material_block,
            )
        except RuntimeError as exc:
            response["center"] = next(item for item in get_centers_with_context() if item["id"] == center_id)
            response["error"] = str(exc)
            return jsonify(response), 502

    response["center"] = next(item for item in get_centers_with_context() if item["id"] == center_id)
    return jsonify(response)


@app.route("/api/health")
def health():
    api_ready = bool(os.getenv("ANTHROPIC_API_KEY"))
    return jsonify(
        {
            "ok": True,
            "database_path": str(DATABASE_PATH),
            "anthropic_api_configured": api_ready,
            "model": DEFAULT_MODEL,
        }
    )


init_db()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
    )
