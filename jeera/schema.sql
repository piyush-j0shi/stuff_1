create extension if not exists vector;

-- identity map: one row per person across jira and slack
create table if not exists people (
    id text primary key,
    name text not null,
    title text,
    email text,
    slack_user_id text
);

create table if not exists jira_projects (
    key text primary key,
    name text,
    board_id int
);

create table if not exists jira_sprints (
    id int primary key,
    project_key text references jira_projects(key),
    name text,
    state text,
    goal text,
    start_date timestamptz,
    end_date timestamptz,
    complete_date timestamptz
);

create table if not exists jira_issues (
    key text primary key,
    project_key text references jira_projects(key),
    type text,
    summary text,
    description text,
    status text,
    status_category text,
    priority text,
    owner text,
    epic_key text,
    sprint_id int,
    sprint_ids int[] default '{}',
    points numeric,
    start_date date,
    due_date date,
    flagged boolean default false,
    labels text[] default '{}',
    created timestamptz,
    updated timestamptz,
    resolved timestamptz,
    url text
);

create table if not exists jira_links (
    blocker_key text,
    blocked_key text,
    primary key (blocker_key, blocked_key)
);

create table if not exists jira_comments (
    id text primary key,
    issue_key text references jira_issues(key) on delete cascade,
    author text,
    body text,
    created timestamptz
);

create table if not exists slack_conversations (
    id text primary key,
    name text,
    kind text,
    purpose text
);

create table if not exists slack_messages (
    channel_id text,
    ts text,
    thread_ts text,
    author text,
    text text,
    posted_at timestamptz,
    jira_keys text[] default '{}',
    mentions text[] default '{}',
    reply_count int default 0,
    latest_reply text,
    url text,
    primary key (channel_id, ts)
);

-- searchable text from slack threads and jira issues
create table if not exists chunks (
    id bigserial primary key,
    ref text unique not null,
    source text not null,
    channel text,
    title text,
    content text not null,
    people text[] default '{}',
    jira_keys text[] default '{}',
    posted_at timestamptz,
    url text,
    content_hash text,
    embedding vector(1536)
);

create index if not exists chunks_embedding_idx on chunks using hnsw (embedding vector_cosine_ops);

create table if not exists health_scores (
    project_key text,
    day date,
    score int,
    details jsonb,
    primary key (project_key, day)
);

create table if not exists sync_runs (
    id bigserial primary key,
    source text,
    started timestamptz default now(),
    finished timestamptz,
    stats jsonb
);

-- every question the agent answers, with the tools it used
create table if not exists agent_runs (
    id bigserial primary key,
    created timestamptz default now(),
    asker text,
    channel text,
    question text,
    answer text,
    tools jsonb,
    seconds numeric,
    tokens_in int,
    tokens_out int,
    error text
);

-- threads the bot answered in, so follow ups there need no @mention
create table if not exists bot_threads (
    channel_id text,
    thread_ts text,
    created timestamptz default now(),
    primary key (channel_id, thread_ts)
);

create table if not exists pending_changes (
    thread_id text primary key,
    channel_id text,
    thread_ts text,
    card_ts text,
    asker text,
    asker_slack text,
    place jsonb,
    status text default 'waiting',
    decided_by text,
    created timestamptz default now(),
    decided timestamptz
);
