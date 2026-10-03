-- Турниры
create table if not exists quiz_tournaments (
    id bigserial primary key,
    chat_id bigint not null,
    started_at timestamptz default now(),
    finished_at timestamptz,
    prize numeric(10,4) default 0.30,
    status text default 'lobby',
    winner_id bigint
);
alter table quiz_tournaments disable row level security;

create table if not exists quiz_tournament_players (
    tournament_id bigint not null,
    user_id bigint not null,
    correct int default 0,
    primary key (tournament_id, user_id)
);
alter table quiz_tournament_players disable row level security;

-- Спонсорские вопросы
create table if not exists quiz_sponsor_questions (
    id bigserial primary key,
    sponsor_id bigint not null,
    chat_id bigint,
    question text not null,
    answer text not null,
    position int,
    used boolean default false,
    created_at timestamptz default now()
);
alter table quiz_sponsor_questions disable row level security;

-- Колонка kind в quiz_invoices
alter table quiz_invoices add column if not exists kind text default 'deposit';
