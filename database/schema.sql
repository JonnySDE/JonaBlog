-- =============================================================================
-- jonablog · Base de datos PostgreSQL (Neon)
-- Ejecutar UNA vez en el SQL Editor de Neon. Es seguro repetirlo (IF NOT EXISTS).
--
-- Tablas:
--   publications  -> la usa la API 2 (Generador): historial de publicaciones
--   events        -> la usa la API 3 (Métricas): eventos anónimos de lectura
-- La API 1 (CMS) NO guarda nada: lee WordPress directamente.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- API 2 · PUBLICATIONS
-- status:        REQUESTED -> ACCEPTED | FAILED
-- vercel_state:  PENDING -> QUEUED/INITIALIZING -> BUILDING -> READY|ERROR|CANCELED
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS publications (
    publication_id   UUID PRIMARY KEY,                       -- UUID v7 generado por la API
    status           TEXT NOT NULL
                     CHECK (status IN ('REQUESTED', 'ACCEPTED', 'FAILED')),
    trigger          TEXT NOT NULL
                     CHECK (trigger IN ('manual', 'scheduled')),
    requested_by     TEXT NOT NULL
                     CHECK (requested_by IN ('admin', 'cron')),
    reason           TEXT CHECK (char_length(reason) <= 200),
    content_version  TEXT CHECK (content_version ~ '^[0-9a-f]{64}$'),  -- SHA-256 hex
    vercel_job_id    TEXT,
    vercel_state     TEXT
                     CHECK (vercel_state IN ('PENDING', 'QUEUED', 'INITIALIZING',
                                             'BUILDING', 'READY', 'ERROR', 'CANCELED')),
    deployment_url   TEXT,
    error            JSONB,                                  -- {code, message}
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Para "¿hubo una publicación aceptada hace poco?" y para el historial
CREATE INDEX IF NOT EXISTS idx_publications_created
    ON publications (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_publications_status_created
    ON publications (status, created_at DESC);

-- -----------------------------------------------------------------------------
-- API 3 · EVENTS  (anónimos: sin IP, sin user-agent, sin cookies)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    event_id         UUID PRIMARY KEY,                       -- evita duplicados (idempotencia)
    type             TEXT NOT NULL CHECK (type IN ('pageview', 'read')),
    path             TEXT NOT NULL CHECK (path LIKE '/%' AND char_length(path) <= 200),
    slug             TEXT CHECK (char_length(slug) <= 100),
    session_id       UUID NOT NULL,                          -- aleatorio, vive solo en la pestaña
    referrer_domain  TEXT CHECK (char_length(referrer_domain) <= 100),
    device           TEXT NOT NULL CHECK (device IN ('mobile', 'tablet', 'desktop')),
    read_seconds     INTEGER CHECK (read_seconds BETWEEN 0 AND 3600),
    scroll_percent   INTEGER CHECK (scroll_percent BETWEEN 0 AND 100),
    occurred_at      TIMESTAMPTZ NOT NULL DEFAULT now(),     -- lo asigna el servidor
    -- read_seconds y scroll_percent solo existen en eventos "read"
    CONSTRAINT read_fields_only_on_read
        CHECK (type = 'read' OR (read_seconds IS NULL AND scroll_percent IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_events_occurred   ON events (occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_slug_time  ON events (slug, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_session    ON events (session_id, occurred_at);

-- Retención: los eventos de más de 12 meses los borra la API 2 en su
-- sincronización diaria (/generator/v1/sync). Para hacerlo a mano:
--   DELETE FROM events WHERE occurred_at < now() - interval '12 months';
