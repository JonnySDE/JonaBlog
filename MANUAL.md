# Manual de conexión · jonablog

Sigue los pasos **en orden** y no te saltes ninguno. Al final tendrás: WordPress → API del CMS → Astro en Vercel → sitio publicado, con la API del Generador publicando y la API de Métricas (GraphQL) midiendo lecturas.

## Qué tienes en la carpeta

```
jonablog/
├── api/                      ← Proyecto Vercel #1 (las 3 APIs en FastAPI)
│   ├── index.py              ← Junta las 3 APIs
│   ├── common.py             ← Errores, tokens, base de datos, cliente de WordPress
│   ├── api1_cms.py           ← API 1: CMS  (/cms/v1)
│   ├── api2_generator.py     ← API 2: Generador (/generator/v1)
│   ├── api3_metrics.py       ← API 3: Métricas GraphQL (/metrics/v1/graphql)
│   ├── requirements.txt
│   ├── vercel.json           ← Cron diario
│   └── .env.example
├── database/schema.sql       ← Tablas de PostgreSQL
├── web/                      ← Proyecto Vercel #2 (sitio Astro)
└── MANUAL.md                 ← Este archivo
```

## Cómo se conecta todo (para que no te pierdas)

```
WordPress.com → API 1 (CMS) → Astro (build en Vercel) → sitio HTML estático
                    ↑                ↑
              API 2 (Generador) ── Deploy Hook ──┘     (API 2 guarda historial en Postgres)
Lector → script del sitio → API 3 (GraphQL) → Postgres (eventos anónimos)
```

Datos que vas a ir juntando (anótalos en un bloc de notas, **no los subas a GitHub**):

| Dato | Lo obtienes en el paso |
|---|---|
| `WORDPRESS_SITE` | 2 |
| `DATABASE_URL` | 3 |
| `CMS_API_TOKEN`, `ADMIN_API_TOKEN`, `CRON_SECRET` | 4 |
| URL de la API en Vercel | 8 |
| URL del sitio en Vercel | 9 |
| `VERCEL_DEPLOY_HOOK_URL`, `VERCEL_API_TOKEN`, `VERCEL_WEB_PROJECT_ID` | 11 |

---

## Paso 1. Prepara tu computadora

1. Cuentas gratuitas: **GitHub**, **Vercel** (entra con tu GitHub), **Neon** (neon.tech) y **WordPress.com**.
2. Instala **Python 3.12 o superior** (python.org). En Windows marca *"Add Python to PATH"*.
3. Instala **Node.js 20 o superior** (nodejs.org).
4. Instala **Git**. En Windows se instala también **Git Bash**: úsalo para todos los comandos de este manual (los `curl` están escritos para Bash).

Comprueba las versiones:

```bash
python --version     # o python3 --version
node --version
git --version
```

---

## Paso 2. WordPress.com: crea contenido de prueba

1. Entra a tu sitio de WordPress.com (el que ya tienes, algo como `blog72076.wordpress.com`).
2. Publica **un artículo** con: título, **una categoría**, **una etiqueta** e **imagen destacada**. Debe quedar en estado **Publicado** (no borrador).
3. Publica **una página** (por ejemplo "Acerca de").
4. Comprueba que la API de WordPress responde. Abre en el navegador (cambia por tu sitio):

   `https://public-api.wordpress.com/wp/v2/sites/TU-SITIO.wordpress.com/posts`

   Debes ver un JSON con tu artículo. Si ves un error, tu sitio no es público o el nombre está mal.
5. Anota el valor de **`WORDPRESS_SITE`** = `TU-SITIO.wordpress.com` (sin `https://`).

---

## Paso 3. Neon: crea la base de datos PostgreSQL

1. En neon.tech: **Create project** → nombre `jonablog` → región cercana (por ejemplo AWS US East) → Create.
2. En el panel del proyecto pulsa **Connect**. Activa la opción **Connection pooling** y copia la cadena que empieza con `postgresql://...`. Su dirección contiene `-pooler`. **Esa es tu `DATABASE_URL`**.
3. En el menú izquierdo abre **SQL Editor**.
4. Abre el archivo `database/schema.sql`, copia **todo** su contenido, pégalo en el editor y pulsa **Run**.
5. Comprueba: en **Tables** deben aparecer `publications` y `events`.

---

## Paso 4. Genera tus 3 tokens

Cada token es una contraseña larga. Genera tres **distintos**:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Ejecútalo tres veces y anota:

| Token | Para qué sirve |
|---|---|
| `CMS_API_TOKEN` | Solo lectura de contenido (lo usa Astro) |
| `ADMIN_API_TOKEN` | Operar el Generador y consultar métricas (lo usas tú) |
| `CRON_SECRET` | Lo usa la tarea programada diaria |

---

## Paso 5. Corre las APIs en tu computadora

```bash
cd jonablog/api
python -m venv .venv
```

Activa el entorno virtual:

- Git Bash (Windows): `source .venv/Scripts/activate`
- Mac / Linux: `source .venv/bin/activate`
- PowerShell: `.venv\Scripts\Activate.ps1`

Instala y configura:

```bash
pip install -r requirements.txt
cp .env.example .env
```

Abre `api/.env` con un editor y rellena: `WORDPRESS_SITE`, los tres tokens y `DATABASE_URL`. Deja `SITE_ORIGIN=http://localhost:4321`. Las cuatro variables `VERCEL_*` déjalas vacías por ahora.

Arranca:

```bash
uvicorn index:app --reload --port 8000
```

### Pruebas (en otra terminal Git Bash)

```bash
# Salud de las tres APIs (sin token). Cada una debe decir "status":"ok"
curl http://localhost:8000/cms/v1/health
curl http://localhost:8000/generator/v1/health
curl http://localhost:8000/metrics/v1/health

# API 1: tus artículos (cambia TU_CMS_TOKEN)
curl -H "Authorization: Bearer TU_CMS_TOKEN" "http://localhost:8000/cms/v1/posts"

# API 1: versión del contenido
curl -H "Authorization: Bearer TU_CMS_TOKEN" http://localhost:8000/cms/v1/content-version
```

Si `cms/health` falla, revisa `WORDPRESS_SITE`. Si `generator/health` falla, revisa `DATABASE_URL`.

También puedes abrir `http://localhost:8000/docs` para probar las APIs REST desde el navegador.

### Probar la API de Métricas (GraphQL)

Enviar un evento (simula al navegador del lector; el encabezado `Origin` debe coincidir con `SITE_ORIGIN`):

```bash
curl -X POST http://localhost:8000/metrics/v1/graphql \
  -H "Content-Type: application/json" \
  -H "Origin: http://localhost:4321" \
  -d '{"query":"mutation($i: EventInput!){ recordEvent(input: $i){ status } }","variables":{"i":{"eventId":"5b8f1c1e-3c1a-4b6e-9d52-0a7e8f2b6c11","type":"pageview","path":"/blog/prueba/","slug":"prueba","sessionId":"9c2a7d10-5e44-4f7b-8a31-2b6d9e0f4a77"}}}'
```

Debe responder `"status":"accepted"`. Si lo repites responde `"duplicate"` (así funciona la idempotencia).

Consultar el resumen (solo administrador):

```bash
curl -X POST http://localhost:8000/metrics/v1/graphql \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer TU_ADMIN_TOKEN" \
  -d '{"query":"{ summary { pageviews sessions avgReadSeconds devices { mobile tablet desktop } topSources { source sessions } } posts { slug pageviews } sources { source sessions } }"}'
```

Otras consultas disponibles: `post(slug: "mi-articulo") { pageviews daily { date pageviews } sources { source sessions } }` y rangos con `summary(from: "2026-10-01", to: "2026-10-15")`.

> Nota: tu `curl` de pruebas se envía con `User-Agent: curl`, que el servidor considera bot y **rechaza** los eventos (`BOT_REJECTED`). Por eso agrega `-A "Mozilla/5.0 (Windows NT 10.0) Chrome/120"` a la mutación si te sale ese error.

Cuando todo responda bien, detén el servidor con `Ctrl + C`.

---

## Paso 6. (Opcional) Prueba el sitio en tu computadora

```bash
cd ../web
cp .env.example .env
```

Edita `web/.env`: `CMS_API_TOKEN` = tu token de CMS. Mantén las URLs `localhost:8000`. Con el servidor de la API **encendido en otra terminal**:

```bash
npm install
npm run dev
```

Abre `http://localhost:4321`. Verás tus artículos de WordPress.

---

## Paso 7. Sube el proyecto a GitHub

1. En GitHub crea un repositorio **vacío** llamado `jonablog` (privado o público).
2. En la carpeta `jonablog/`:

```bash
git init
git add .
git commit -m "jonablog: APIs, base de datos y sitio"
git branch -M main
git remote add origin https://github.com/TU-USUARIO/jonablog.git
git push -u origin main
```

3. Verifica en GitHub que **no** aparezca ningún archivo `.env` (el `.gitignore` ya lo evita).

---

## Paso 8. Vercel, proyecto 1: las APIs

1. En vercel.com: **Add New… → Project** → importa el repositorio `jonablog`.
2. En **Root Directory** pulsa *Edit* y elige **`api`**.
3. **Framework Preset**: FastAPI (si no aparece, deja *Other*; Vercel detecta `index.py`).
4. Abre **Environment Variables** y agrega estas (nombre → valor):

| Nombre | Valor |
|---|---|
| `WORDPRESS_SITE` | `tusitio.wordpress.com` |
| `CMS_API_TOKEN` | tu token |
| `ADMIN_API_TOKEN` | tu token |
| `CRON_SECRET` | tu token |
| `DATABASE_URL` | tu cadena de Neon |
| `SITE_ORIGIN` | `https://temporal.com` *(se corrige en el Paso 10)* |

5. Pulsa **Deploy**.
6. Cuando termine, copia el dominio del proyecto (ejemplo: `https://jonablog-api.vercel.app`). Es tu **URL de la API**.
7. Prueba:

```bash
curl https://TU-API.vercel.app/cms/v1/health
curl https://TU-API.vercel.app/generator/v1/health
curl https://TU-API.vercel.app/metrics/v1/health
```

Las tres deben decir `"status":"ok"`. La primera llamada puede tardar unos segundos (arranque en frío).

---

## Paso 9. Vercel, proyecto 2: el sitio Astro

1. **Add New… → Project** → importa **el mismo** repositorio `jonablog`.
2. **Root Directory**: **`web`**. **Framework Preset**: Astro.
3. Variables de entorno:

| Nombre | Valor |
|---|---|
| `CMS_API_URL` | `https://TU-API.vercel.app/cms/v1` |
| `CMS_API_TOKEN` | el **mismo** token de CMS (solo lectura, nunca el de administrador) |
| `PUBLIC_METRICS_URL` | `https://TU-API.vercel.app/metrics/v1/graphql` |

4. **Deploy**. Si el build falla con un error 401, el `CMS_API_TOKEN` no coincide con el de la API.
5. Copia el dominio del sitio (ejemplo: `https://jonablog-web.vercel.app`) y ábrelo: verás tu blog.

---

## Paso 10. Corrige `SITE_ORIGIN` en la API

`SITE_ORIGIN` debe ser **exactamente** el dominio del sitio, con `https://` y **sin diagonal final**. Es lo que permite que el navegador de tus lectores envíe métricas.

1. Proyecto **api** → Settings → Environment Variables.
2. Edita `SITE_ORIGIN` = `https://jonablog-web.vercel.app` (el tuyo).
3. Para que tome efecto: pestaña **Deployments** → los tres puntos del último deployment → **Redeploy**.

> Cada vez que cambies una variable de entorno en Vercel debes hacer **Redeploy**.

---

## Paso 11. Conecta el Generador con Vercel

La API del Generador necesita tres datos del proyecto **web**:

1. **Deploy Hook** → proyecto **web** → Settings → **Git** → sección *Deploy Hooks* → nombre `generador`, rama `main` → **Create Hook** → copia la URL. Es **`VERCEL_DEPLOY_HOOK_URL`**. Es secreta: quien la tenga puede lanzar builds.
2. **Project ID** → proyecto **web** → Settings → **General** → *Project ID*. Es **`VERCEL_WEB_PROJECT_ID`**.
3. **Token de Vercel** → clic en tu avatar → Settings → **Tokens** → Create → nombre `jonablog-generador`. Cópialo (solo se muestra una vez). Es **`VERCEL_API_TOKEN`**.
4. Si tu proyecto está dentro de un *Team* (no en tu cuenta personal), agrega también `VERCEL_TEAM_ID` (Team Settings → General → Team ID). En cuenta personal no hace falta.

Agrega esas variables al proyecto **api** y haz **Redeploy** del proyecto api.

---

## Paso 12. Prueba completa: publicar un artículo

1. En WordPress publica un artículo nuevo.
2. Pide la publicación (cambia `TU_ADMIN_TOKEN` y la URL):

```bash
curl -X POST https://TU-API.vercel.app/generator/v1/publications \
  -H "Authorization: Bearer TU_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason":"Primera publicación","only_if_changed":false}'
```

Debe responder `201` con `"status":"ACCEPTED"` y un `publication_id`. Cópialo.

3. Revisa el avance del build (repite hasta ver `READY`):

```bash
curl -H "Authorization: Bearer TU_ADMIN_TOKEN" \
  https://TU-API.vercel.app/generator/v1/publications/EL_PUBLICATION_ID
```

El campo `vercel_state` avanza: `PENDING → BUILDING → READY`. Si llega a `ERROR`, el detalle viene en `error`; mira también Vercel → proyecto web → Deployments.

4. Abre tu sitio: el artículo nuevo ya aparece.
5. Abre un artículo en el sitio, espera unos segundos, desplázate y cierra la pestaña. Consulta las métricas (Paso 5, con tu URL de Vercel y tu token de administrador): verás las vistas y el tiempo de lectura.

Respuestas que debes conocer:

| Respuesta | Significado |
|---|---|
| `409 PUBLICATION_TOO_RECENT` | Pediste otra publicación hace menos de un minuto; espera lo que indica `detail`. |
| `200 NO_CHANGES` | Usaste `"only_if_changed": true` y no hay nada nuevo. |
| `503 HOOK_NOT_CONFIGURED` | Falta `VERCEL_DEPLOY_HOOK_URL` (o no hiciste Redeploy). |
| `502 VERCEL_HOOK_ERROR` | Vercel rechazó el hook (URL revocada o incorrecta). |

Para ver el historial: `GET /generator/v1/publications` con tu token de administrador.

---

## Paso 13. Tarea programada (ya está lista)

El archivo `api/vercel.json` ya incluye un cron diario que llama a `/generator/v1/sync`. Vercel envía automáticamente el encabezado `Authorization: Bearer <CRON_SECRET>` usando la variable `CRON_SECRET` que ya configuraste.

Verifícalo en Vercel → proyecto **api** → Settings → **Cron Jobs**: debe aparecer `/generator/v1/sync`. Puedes lanzarlo a mano desde ahí o con:

```bash
curl -H "Authorization: Bearer TU_CRON_SECRET" https://TU-API.vercel.app/generator/v1/sync
```

Responde `"published": false` si no hay cambios y `true` si publicó. En el plan gratuito corre **una vez al día** sin hora exacta; para publicar al instante usa el Paso 12. El sync también borra los eventos de métricas de más de 12 meses.

---

## Paso 14. Enlaza tu landing de GitHub Pages con el blog

Cuando el blog funcione, en tu landing cambia el enlace "Blog" para que apunte a `https://jonablog-web.vercel.app` (o a tu dominio propio, si luego se lo agregas en Vercel → proyecto web → Settings → Domains). Haz commit y push del repositorio de la landing.

---

## Tu rutina diaria

1. Escribes y publicas en WordPress.
2. Ejecutas el `curl` del Paso 12 (o lo guardas en Postman).
3. Esperas el estado `READY` y el sitio ya está actualizado.

---

## Problemas comunes

| Síntoma | Causa probable | Solución |
|---|---|---|
| `401 UNAUTHORIZED` | Token mal copiado o falta `Bearer ` | Formato exacto: `Authorization: Bearer TOKEN`. Revisa espacios. |
| `cms/health` da `503` | `WORDPRESS_SITE` mal escrito o sitio privado | Repite la prueba del navegador del Paso 2. |
| `generator/health` da `503` / `DB_UNAVAILABLE` | `DATABASE_URL` mal o base dormida | Pega de nuevo la cadena de Neon; reintenta (Neon "despierta" en unos segundos). |
| Error `relation "events" does not exist` | No ejecutaste `schema.sql` | Repite el Paso 3. |
| Build de Astro falla con 401 | `CMS_API_TOKEN` distinto entre api y web | Usa el mismo valor en ambos proyectos. |
| Build falla con 502/503 | WordPress tardó o falló | Reintenta el deploy. |
| Eventos con `ORIGIN_NOT_ALLOWED` | `SITE_ORIGIN` no coincide | Debe ser el dominio exacto del sitio, sin `/` final, y haber hecho Redeploy. |
| Eventos con `BOT_REJECTED` | Cliente identificado como bot (curl, scripts) | Agrega `-A "Mozilla/5.0 ..."` en tus pruebas. |
| Cambié una variable y no pasa nada | Falta Redeploy | Deployments → Redeploy. |
| El sitio no muestra mi artículo nuevo | No se ha publicado | Ejecuta el Paso 12. |
| Se desactualizó el estado (`X-State-Stale: true`) | Vercel no respondió a la consulta | Repite la consulta en unos segundos. |

---

## Lo que probé y lo que debes verificar tú

**Probado aquí** con PostgreSQL 16 real y un WordPress y un Vercel simulados: las tres APIs (autenticación, validaciones, errores, límite de 1 publicación por minuto, detección de cambios, idempotencia de eventos, privacidad, consultas GraphQL, CORS) y que el sitio Astro compila generando todas sus páginas.

**No pude probar contra los servicios reales** (WordPress.com, Vercel, Neon). Son los puntos del Anexo B de tu documento; si algo falla, empieza por aquí:

- Que WordPress.com acepte los parámetros usados (`_embed`, `_fields`, `hide_empty`, `slug`). Lo comprobarás en la prueba del Paso 5.
- Que el Deploy Hook responda con `job.id` (si no, el build funciona igual pero `vercel_job_id` queda vacío).
- Que Vercel marque los builds del hook con origen `api-trigger-git-deploy`; si `vercel_state` se queda en `PENDING` para siempre, revisa `VERCEL_API_TOKEN`, `VERCEL_WEB_PROJECT_ID` y ese origen en `api2_generator.py` (función `refresh_from_vercel`).
- Los límites actuales de los planes gratuitos de Vercel y Neon.
