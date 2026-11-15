-- Bills read by the model, kept as drafts until a person confirms them.
--
-- `fields` is what the model read (normalised in code), `flags` the fields a
-- person must look at. Nothing goes into debtor, phone or debt until confirm,
-- which stores the person's own `confirmed_fields` and the new debt's id.
-- RLS is on with no policy: only the server (the database owner) reads these.

create table public.bill_draft (
    id text primary key check (id ~ '^bill_[0-9a-f]{32}$'),
    created_at timestamptz not null default now(),
    storage_key text not null,
    content_type text not null
        check (content_type in ('image/png', 'image/jpeg', 'image/webp', 'application/pdf')),
    sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
    fields jsonb not null,
    flags jsonb not null default '{}'::jsonb,
    status text not null default 'draft' check (status in ('draft', 'confirmed')),
    confirmed_at timestamptz,
    confirmed_fields jsonb,
    debt_id uuid references public.debt (id) on delete set null,
    check ((status = 'confirmed') = (confirmed_at is not null))
);

alter table public.bill_draft enable row level security;

-- The uploaded files: a private bucket, read and written only with the service key.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'bills', 'bills', false, 10485760,
    array['image/png', 'image/jpeg', 'image/webp', 'application/pdf']
)
on conflict (id) do nothing;
