# Adaptador de Entrenamientos

Aplicacion web para pegar un entrenamiento original y obtener tres adaptaciones distintas, una por centro, usando Claude por API. La app guarda:

- configuracion editable de cada centro
- memoria permanente por centro
- historial de generaciones

Todo queda persistido en `instance/app.db`, de forma que la memoria se conserva entre usos.

## Que hace esta primera version

- Pegar un entrenamiento completo.
- Elegir uno, dos o tres centros antes de generar.
- Generar versiones adaptadas solo para los centros seleccionados.
- Editar independientemente personas por clase, material disponible y memoria permanente de cada centro.
- Guardar memoria permanente por centro para que se reutilice en futuras generaciones.
- Mantener calentamiento y movilidad siempre iguales al original.
- Regenerar un centro concreto usando ese nuevo feedback.
- Mostrar la salida en texto plano lista para copiar y pegar, incluyendo entrenamiento y briefing.
- Generar un briefing por centro con el formato estandar de 5 bloques de CrossFit Metropolitano.

## Arquitectura

- `app.py`: app Flask, endpoints, logica de persistencia y llamada a Claude.
- `templates/index.html`: interfaz principal.
- `static/app.js`: interacciones del frontend.
- `static/styles.css`: estilos de la interfaz.
- `instance/app.db`: base de datos SQLite creada automaticamente.

La integracion con Claude usa la Messages API oficial de Anthropic (`POST /v1/messages`) con `x-api-key` y `anthropic-version: 2023-06-01`.

Referencias oficiales:

- [Anthropic Messages API](https://docs.anthropic.com/en/api/messages)
- [Anthropic API Overview](https://docs.anthropic.com/en/api/getting-started)

## Puesta en marcha

1. Crear entorno virtual:

```bash
python3 -m venv .venv
```

2. Instalar dependencias:

```bash
./.venv/bin/pip install -r requirements.txt
```

3. Configurar variables de entorno:

```bash
cp .env.example .env
```

Edita `.env` y define al menos:

```env
ANTHROPIC_API_KEY=tu_clave
CLAUDE_MODEL=claude-sonnet-4-20250514
```

4. Ejecutar la app:

```bash
./.venv/bin/python app.py
```

5. Abrir en navegador:

```text
http://127.0.0.1:5000
```

## Endpoints utiles

- `GET /api/health`: comprueba si la base de datos y la API estan configuradas.
- `POST /api/generate`: genera para los centros seleccionados.
- `POST /api/centers/<id>`: actualiza la configuracion de un centro.
- `POST /api/centers/<id>/feedback`: guarda la memoria permanente y opcionalmente regenera.

## Publicacion para equipo (staging + produccion)

Este repo ya incluye lo necesario para desplegar en Render:

- `Procfile` para arrancar con Gunicorn.
- `render.yaml` con dos servicios web:
  - `metropolitano-training-app-staging` (rama `develop`)
  - `metropolitano-training-app-production` (rama `main`)
- Disco persistente en ambos servicios para mantener `instance/app.db` entre reinicios.

### Paso a paso (una vez)

1. Sube este proyecto a GitHub/GitLab.
2. En Render:
   - `New` -> `Blueprint`
   - selecciona este repo (detecta `render.yaml`)
3. Define variable secreta en cada servicio:
   - `ANTHROPIC_API_KEY`
4. (Opcional recomendado) Asigna dominio propio al servicio de produccion.

### Flujo de trabajo recomendado

1. Desarrollas cambios en ramas feature y los fusionas en `develop`.
2. Render despliega automaticamente `staging`.
3. Validas con el equipo interno.
4. Fusionas `develop` a `main`.
5. Render despliega automaticamente `production`.

### Comandos locales utiles

```bash
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python app.py
```

O en modo produccion local:

```bash
./.venv/bin/gunicorn --workers=2 --threads=4 --timeout=120 --bind=0.0.0.0:5000 app:app
```

## Como ampliar rapido

La base ya esta preparada para crecer sin rehacerla. Algunas siguientes mejoras naturales:

- autenticacion de usuarios
- multiples plantillas de prompt
- versionado y archivado de feedback
- exportacion a PDF o Word
- panel de pruebas A/B por centro
- aprobacion manual antes de guardar una version
- clasificacion del feedback por categorias
