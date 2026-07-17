-- ============================================================
-- EcoSure Food Safety Assessments — Supabase schema
-- Run in Supabase SQL editor. Safe to re-run (idempotent-ish).
-- ============================================================

create extension if not exists "pgcrypto";  -- for gen_random_uuid()

-- One row per report ------------------------------------------------------
create table if not exists ecosure_assessments (
  id                     uuid primary key default gen_random_uuid(),
  unit_number            text not null,
  restaurant_name        text,
  store_type             text,
  brand                  text,
  address                text,
  city                   text,
  state                  text,
  zip                    text,
  start_datetime         timestamptz,
  end_datetime           timestamptz,
  advisor_id             text,
  visit                  int,
  score_pct              int,
  risk_level             text,           -- 'Moderate Risk', 'High Risk', ...
  report_generated       timestamptz,
  manager_name           text,
  manager_signature_date timestamptz,
  summary                jsonb,          -- per-category count matrix
  source_pdf_path        text,           -- Storage path of the raw PDF
  email_message_id       text,           -- Gmail msg id (dedup / audit)
  parse_warnings         jsonb,          -- non-empty if the parser suspects template drift
  created_at             timestamptz default now(),
  -- one report per unit + evaluation start (retries upsert instead of dupe)
  unique (unit_number, start_datetime)
);

create index if not exists idx_assess_unit  on ecosure_assessments (unit_number);
create index if not exists idx_assess_start on ecosure_assessments (start_datetime desc);
create index if not exists idx_assess_risk  on ecosure_assessments (risk_level);

-- Actionable standards = the violations ----------------------------------
create table if not exists ecosure_violations (
  id            uuid primary key default gen_random_uuid(),
  assessment_id uuid not null references ecosure_assessments(id) on delete cascade,
  category      text,
  code          text,           -- 'J-CS.4'
  question      text,
  priority      text,           -- 'Minor' | 'Major' | 'Critical' | 'Imminent Health Risk'
  response      text,           -- 'No'
  findings      jsonb,          -- [{issue, detail}, ...]
  created_at    timestamptz default now()
);
create index if not exists idx_viol_assess   on ecosure_violations (assessment_id);
create index if not exists idx_viol_priority on ecosure_violations (priority);

-- Finding photos ----------------------------------------------------------
create table if not exists ecosure_photos (
  id             uuid primary key default gen_random_uuid(),
  assessment_id  uuid not null references ecosure_assessments(id) on delete cascade,
  violation_code text,          -- 'J-CS.4' (nullable if unassociated)
  photo_index    int,
  storage_path   text,          -- path within the storage bucket
  public_url     text,
  page           int,
  created_at     timestamptz default now()
);
create index if not exists idx_photo_assess on ecosure_photos (assessment_id);

-- Detailed standards = the full pass/fail Q&A ----------------------------
create table if not exists ecosure_detailed_standards (
  id            uuid primary key default gen_random_uuid(),
  assessment_id uuid not null references ecosure_assessments(id) on delete cascade,
  category      text,
  code          text,           -- 'J-IHR.1'
  question      text,
  answer        text,           -- 'Yes' | 'No' | null
  passed        boolean,        -- answer = 'Yes'
  created_at    timestamptz default now()
);
create index if not exists idx_detail_assess on ecosure_detailed_standards (assessment_id);

-- Convenience view for a Lovable list screen -----------------------------
create or replace view ecosure_assessment_overview as
select
  a.id, a.unit_number, a.restaurant_name, a.city, a.state,
  a.start_datetime, a.score_pct, a.risk_level,
  (a.summary->'total'->>'critical')::int as critical_count,
  (a.summary->'total'->>'major')::int    as major_count,
  (a.summary->'total'->>'minor')::int    as minor_count,
  (select count(*) from ecosure_violations v where v.assessment_id = a.id) as violation_count,
  (select count(*) from ecosure_photos    p where p.assessment_id = a.id) as photo_count
from ecosure_assessments a
order by a.start_datetime desc;
