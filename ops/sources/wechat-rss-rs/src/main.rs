use axum::{
    extract::{Path, Query, State},
    http::{header, StatusCode},
    response::{Html, IntoResponse},
    routing::{get, post},
    Json, Router,
};
use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveDate, Utc};
use html2md::parse_html;
use regex::Regex;
use reqwest::Client;
use rss::Channel;
use rusqlite::{params, Connection};
use scraper::{Html as ScraperHtml, Selector};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs,
    net::SocketAddr,
    path::PathBuf,
    sync::{Arc, Mutex},
};

mod db;

#[derive(Clone)]
struct AppState {
    db_path: PathBuf,
    settings_path: PathBuf,
    db_lock: Arc<Mutex<()>>,
    http: Client,
    auto_runtime: Arc<Mutex<AutoRefreshRuntime>>,
}

#[derive(Debug, Deserialize)]
struct ListQuery {
    subscription_id: Option<i64>,
    days: Option<i64>,
    limit: Option<i64>,
    hours: Option<i64>,
}

#[derive(Debug, Deserialize, Default)]
struct RefreshPayload {
    sample_fetches: Option<i64>,
    sample_interval: Option<f64>,
    days: Option<i64>,
}

#[derive(Debug, Serialize)]
struct Subscription {
    id: i64,
    biz: String,
    name: String,
    feed_url: String,
    enabled: i64,
    created_at: Option<String>,
    updated_at: Option<String>,
    last_refresh_at: Option<String>,
    last_status: Option<String>,
    last_error: Option<String>,
}

#[derive(Debug, Serialize)]
struct Entry {
    id: i64,
    subscription_id: i64,
    guid: String,
    title: String,
    link: String,
    summary: String,
    content_markdown: String,
    published_at: Option<String>,
    published_at_local: Option<String>,
    inserted_at: Option<String>,
    last_seen_at: Option<String>,
    sample_hits: i64,
    subscription_name: Option<String>,
}

#[derive(Debug, Serialize)]
struct FetchRun {
    id: i64,
    subscription_id: i64,
    started_at: String,
    finished_at: Option<String>,
    status: String,
    sample_fetches: i64,
    items_seen: i64,
    items_saved: i64,
    note: Option<String>,
}

fn now_iso() -> String {
    Utc::now().to_rfc3339()
}

const LLM_COST_POLICY: &str = "free_only";
const LLM_AD_ROUTE_ON: &str = "free_longcat";
const LLM_AD_ROUTE_OFF: &str = "off";
const FREE_LLM_ERROR: &str =
    "RSS sidecar only allows free LongCat-Flash-Lite for automatic LLM checks";

#[derive(Clone, Default)]
struct LlmSettings {
    enabled: bool,
    api_base: String,
    api_key: String,
    model: String,
}

impl LlmSettings {
    fn configured(&self) -> bool {
        !self.api_base.trim().is_empty()
            && !self.api_key.trim().is_empty()
            && !self.model.trim().is_empty()
    }

    fn free_allowed(&self) -> bool {
        let base = self.api_base.to_lowercase();
        let model = self.model.to_lowercase();
        base.contains("longcat") && model.contains("longcat-flash-lite")
    }

    fn enabled(&self) -> bool {
        self.enabled && self.configured() && self.free_allowed()
    }

    fn with_payload(mut self, payload: &Value, preserve_masked_key: bool) -> Self {
        if let Some(v) = payload.get("enabled").and_then(|v| v.as_bool()) {
            self.enabled = v;
        }
        if let Some(v) = payload.get("api_base").and_then(|v| v.as_str()) {
            self.api_base = v.trim().to_string();
        }
        if let Some(v) = payload.get("api_key").and_then(|v| v.as_str()) {
            let incoming = v.trim();
            if !preserve_masked_key || (!incoming.is_empty() && !is_masked_secret(incoming)) {
                self.api_key = incoming.to_string();
            }
        }
        if let Some(v) = payload.get("model").and_then(|v| v.as_str()) {
            self.model = v.trim().to_string();
        }
        self
    }

    fn public_json(&self) -> Value {
        json!({
            "enabled": self.enabled,
            "api_base": self.api_base,
            "api_key": masked_secret(&self.api_key),
            "api_key_present": !self.api_key.trim().is_empty(),
            "model": self.model,
            "cost_policy": LLM_COST_POLICY,
            "auto_active": self.enabled(),
        })
    }

    fn stored_json(&self) -> Value {
        json!({
            "enabled": self.enabled,
            "api_base": self.api_base,
            "api_key": self.api_key,
            "model": self.model,
            "cost_policy": LLM_COST_POLICY,
        })
    }

    fn chat_completions_url(&self) -> String {
        let mut url = self.api_base.trim_end_matches('/').to_string();
        if !url.ends_with("/chat/completions") {
            url.push_str("/chat/completions");
        }
        url
    }

    fn ad_route_note(&self) -> &'static str {
        if self.enabled() {
            LLM_AD_ROUTE_ON
        } else {
            LLM_AD_ROUTE_OFF
        }
    }
}

#[derive(Debug, Clone)]
struct AutoRefreshConfig {
    enabled: bool,
    interval_seconds: i64,
}

impl Default for AutoRefreshConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            interval_seconds: 3600,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
struct AutoRefreshRuntime {
    thread_alive: bool,
    running: bool,
    last_run_at: Option<String>,
    next_run_at: Option<String>,
    last_status: String,
    last_message: String,
}

impl Default for AutoRefreshRuntime {
    fn default() -> Self {
        Self {
            thread_alive: false,
            running: false,
            last_run_at: None,
            next_run_at: None,
            last_status: "idle".to_string(),
            last_message: String::new(),
        }
    }
}

fn conn(path: &PathBuf) -> Result<Connection, String> {
    Connection::open(path).map_err(|e| e.to_string())
}

fn read_settings(path: &PathBuf) -> Value {
    match fs::read_to_string(path) {
        Ok(s) => serde_json::from_str(&s).unwrap_or_else(|_| json!({})),
        Err(_) => json!({}),
    }
}

fn load_llm_settings_compat(path: &PathBuf) -> LlmSettings {
    let settings = read_settings(path);
    let root = settings.as_object().cloned().unwrap_or_default();
    let llm_obj = settings
        .get("llm")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();

    let get_field = |name: &str| -> String {
        root.get(name)
            .and_then(|v| v.as_str())
            .or_else(|| llm_obj.get(name).and_then(|v| v.as_str()))
            .unwrap_or("")
            .to_string()
    };

    let enabled = root
        .get("llm_enabled")
        .and_then(|v| v.as_bool())
        .or_else(|| llm_obj.get("enabled").and_then(|v| v.as_bool()))
        .unwrap_or(false);
    LlmSettings {
        enabled,
        api_base: get_field("api_base"),
        api_key: get_field("api_key"),
        model: get_field("model"),
    }
}

fn load_auto_refresh_config(path: &PathBuf) -> AutoRefreshConfig {
    let settings = read_settings(path);
    let enabled = settings
        .get("auto_refresh_enabled")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    let seconds = settings
        .get("auto_refresh_seconds")
        .and_then(|v| v.as_i64())
        .or_else(|| {
            settings
                .get("auto_refresh_minutes")
                .and_then(|v| v.as_i64())
                .map(|m| m * 60)
        })
        .unwrap_or(3600)
        .clamp(5, 86400);
    AutoRefreshConfig {
        enabled,
        interval_seconds: seconds,
    }
}

fn ad_score(title: &str, summary: &str, _content: &str) -> i32 {
    let text = format!("{}\n{}", title, summary).to_lowercase();
    let hard_title = ["八段锦的猛料", "刺痛了多少中国女人"];
    let hard = [
        "-广告-",
        "限时0元",
        "立即领取",
        "报名通道",
        "仅需0元",
        "免费社群陪伴",
    ];
    let soft = [
        "广告",
        "赞助",
        "推广",
        "课程",
        "训练营",
        "0元",
        "报名",
        "扫码",
        "加微信",
        "下单",
        "限时",
        "福利",
        "购课",
        "客服",
        "先到先得",
        "体验营",
    ];
    let mut s = 0_i32;
    for k in hard_title {
        if title.contains(k) {
            s += 10;
        }
    }
    for k in hard {
        if text.contains(k) {
            s += 3;
        }
    }
    for k in soft {
        if text.contains(k) {
            s += 1;
        }
    }
    s
}

async fn llm_is_ad(client: &Client, llm: &LlmSettings, title: &str, summary: &str) -> Option<bool> {
    if !llm.enabled() {
        return None;
    }
    let mut url = llm.api_base.trim_end_matches('/').to_string();
    if !url.ends_with("/chat/completions") {
        url.push_str("/chat/completions");
    }
    let body = json!({
        "model": llm.model,
        "messages": [
            {"role":"system","content":"你是内容审核助手。仅回答 AD 或 NORMAL，不要输出其他内容。"},
            {"role":"user","content": format!("判断这篇公众号文章是否属于广告/推广文。\n标题: {}\n摘要: {}\n如果是广告/推广返 AD，否则返 NORMAL。", title, summary)}
        ],
        "max_tokens": 6,
        "temperature": 0
    });
    let resp = client
        .post(url)
        .bearer_auth(llm.api_key.clone())
        .json(&body)
        .timeout(std::time::Duration::from_secs(30))
        .send()
        .await
        .ok()?;
    let ok = resp.error_for_status().ok()?;
    let parsed: Value = ok.json().await.ok()?;
    let content = parsed
        .get("choices")
        .and_then(|v| v.as_array())
        .and_then(|arr| arr.first())
        .and_then(|x| x.get("message"))
        .and_then(|x| x.get("content"))
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .trim()
        .to_uppercase();
    if content.contains("AD") {
        Some(true)
    } else if content.contains("NORMAL") {
        Some(false)
    } else {
        None
    }
}

fn write_settings(path: &PathBuf, data: &Value) -> Result<(), String> {
    let payload = serde_json::to_string_pretty(data).map_err(|e| e.to_string())?;
    fs::write(path, payload).map_err(|e| e.to_string())
}

fn masked_secret(value: &str) -> String {
    if value.trim().is_empty() {
        String::new()
    } else {
        "********".to_string()
    }
}

fn is_masked_secret(value: &str) -> bool {
    let v = value.trim();
    !v.is_empty() && v.chars().all(|c| c == '*')
}

fn query_param(link: &str, key: &str) -> Option<String> {
    let qpos = link.find('?')?;
    let qs = &link[qpos + 1..];
    for part in qs.split('&') {
        let mut it = part.splitn(2, '=');
        let k = it.next().unwrap_or("").trim();
        let v = it.next().unwrap_or("").trim();
        if k == key && !v.is_empty() {
            return Some(v.to_string());
        }
    }
    None
}

fn parse_guid(link: &str, fallback: &str) -> String {
    let mid = query_param(link, "mid");
    let idx = query_param(link, "idx").or_else(|| query_param(link, "itemidx"));
    match (mid, idx) {
        (Some(m), Some(i)) => return format!("{m}:{i}"),
        (Some(m), None) => return m,
        _ => {}
    }
    if !fallback.trim().is_empty() {
        fallback.trim().to_string()
    } else {
        link.to_string()
    }
}

fn parse_pub_date(s: Option<&str>) -> Option<String> {
    let v = s?.trim();
    if v.is_empty() {
        return None;
    }
    if let Ok(dt) = DateTime::parse_from_rfc2822(v) {
        return Some(dt.with_timezone(&Utc).to_rfc3339());
    }
    if let Ok(dt) = DateTime::parse_from_rfc3339(v) {
        return Some(dt.with_timezone(&Utc).to_rfc3339());
    }
    None
}

fn to_shanghai_time(v: Option<&str>) -> Option<String> {
    let raw = v?.trim();
    if raw.is_empty() {
        return None;
    }
    let dt_utc = if let Ok(dt) = DateTime::parse_from_rfc3339(raw) {
        dt.with_timezone(&Utc)
    } else if let Ok(dt) = DateTime::parse_from_rfc2822(raw) {
        dt.with_timezone(&Utc)
    } else {
        return None;
    };
    let tz = FixedOffset::east_opt(8 * 3600)?;
    Some(
        dt_utc
            .with_timezone(&tz)
            .format("%Y-%m-%d %H:%M:%S")
            .to_string(),
    )
}

async fn fetch_feed_text(client: &Client, url: &str) -> Result<String, String> {
    let mut last_err = String::new();
    for attempt in 1..=3 {
        match client.get(url).send().await {
            Ok(resp) => match resp.error_for_status() {
                Ok(ok) => match ok.text().await {
                    Ok(text) => return Ok(text),
                    Err(e) => last_err = e.to_string(),
                },
                Err(e) => last_err = e.to_string(),
            },
            Err(e) => last_err = e.to_string(),
        }
        let ms = 500_u64 * attempt;
        tokio::time::sleep(std::time::Duration::from_millis(ms)).await;
    }
    Err(last_err)
}

async fn fetch_article_markdown_from_link(client: &Client, url: &str) -> Option<String> {
    let link = url.trim();
    if link.is_empty() {
        return None;
    }
    let resp = client
        .get(link)
        .send()
        .await
        .ok()?
        .error_for_status()
        .ok()?;
    let html = resp.text().await.ok()?;
    let md = parse_html(&html).trim().to_string();
    if md.is_empty() {
        None
    } else {
        Some(md)
    }
}

const YAGE_KIT_PROFILE_URL: &str = "https://yage-ai.kit.com/profile";
const YAGE_KIT_DAILY_URL: &str = "kit://yage/daily";
const YAGE_KIT_WEEKLY_URL: &str = "kit://yage/weekly";

fn is_yage_kit_daily(url: &str) -> bool {
    url.trim().eq_ignore_ascii_case(YAGE_KIT_DAILY_URL)
}

fn is_yage_kit_weekly(url: &str) -> bool {
    url.trim().eq_ignore_ascii_case(YAGE_KIT_WEEKLY_URL)
}

fn extract_yage_date_from_url(url: &str) -> Option<NaiveDate> {
    let marker = "/posts/ai-";
    let idx = url.find(marker)?;
    let start = idx + marker.len();
    if url.len() < start + 10 {
        return None;
    }
    let date_str = &url[start..start + 10];
    NaiveDate::parse_from_str(date_str, "%Y-%m-%d").ok()
}

fn rfc3339_from_date(date: NaiveDate) -> Option<String> {
    let dt = date.and_hms_opt(0, 0, 0)?;
    Some(DateTime::<Utc>::from_naive_utc_and_offset(dt, Utc).to_rfc3339())
}

fn yage_title_from_url(url: &str) -> String {
    if let Some(d) = extract_yage_date_from_url(url) {
        return format!("鸭哥 AI 要闻 {}", d.format("%Y-%m-%d"));
    }
    "鸭哥 AI 要闻".to_string()
}

fn yage_decode_content_field(article_html: &str) -> Option<String> {
    let re = Regex::new(r#""content":"(.*?)","recentPosts""#).ok()?;
    let cap = re.captures(article_html)?;
    let escaped = cap.get(1)?.as_str();
    let wrapped = format!("\"{escaped}\"");
    if let Ok(decoded) = serde_json::from_str::<String>(&wrapped) {
        return Some(decoded);
    }
    Some(
        escaped
            .replace("\\n", "\n")
            .replace("\\\"", "\"")
            .replace("\\/", "/")
            .replace("\\u003c", "<")
            .replace("\\u003e", ">")
            .replace("\\u0026", "&"),
    )
}

fn yage_extract_title_line(markdown: &str) -> String {
    for raw in markdown.lines() {
        let mut s = raw.trim();
        if s.is_empty() {
            continue;
        }
        if s.starts_with('>') {
            continue;
        }
        if s.starts_with("**") && s.ends_with("**") && s.len() > 4 {
            s = s.trim_matches('*').trim();
        }
        if s.starts_with('#') {
            s = s.trim_start_matches('#').trim();
        }
        if !s.is_empty() {
            return s.to_string();
        }
    }
    String::new()
}

async fn fetch_yage_article_markdown(
    client: &Client,
    url: &str,
) -> Option<(String, String, String)> {
    let article_html = client
        .get(url)
        .send()
        .await
        .ok()?
        .error_for_status()
        .ok()?
        .text()
        .await
        .ok()?;
    let raw = yage_decode_content_field(&article_html)?;
    let markdown = parse_html(&raw).trim().to_string();
    if markdown.is_empty() {
        return None;
    }
    let title = {
        let t = yage_extract_title_line(&markdown);
        if t.is_empty() {
            yage_title_from_url(url)
        } else {
            t
        }
    };
    let summary = markdown.chars().take(500).collect::<String>();
    Some((title, markdown, summary))
}

async fn fetch_yage_kit_post_urls(client: &Client, limit: usize) -> Result<Vec<String>, String> {
    let html = client
        .get(YAGE_KIT_PROFILE_URL)
        .send()
        .await
        .map_err(|e| e.to_string())?
        .error_for_status()
        .map_err(|e| e.to_string())?
        .text()
        .await
        .map_err(|e| e.to_string())?;

    let doc = ScraperHtml::parse_document(&html);
    let sel = Selector::parse("a[href]").map_err(|e| e.to_string())?;
    let mut out: Vec<String> = Vec::new();
    for a in doc.select(&sel) {
        let Some(href) = a.value().attr("href") else {
            continue;
        };
        let full = if href.starts_with("https://yage-ai.kit.com/posts/") {
            href.to_string()
        } else if href.starts_with("/posts/") {
            format!("https://yage-ai.kit.com{href}")
        } else {
            continue;
        };
        if out.contains(&full) {
            continue;
        }
        out.push(full);
        if out.len() >= limit {
            break;
        }
    }
    Ok(out)
}

async fn build_yage_daily_entries(days: i64, client: &Client) -> Result<Vec<Entry>, String> {
    let cutoff_date = (Utc::now() - Duration::days(days.max(1))).date_naive();
    let mut items: Vec<Entry> = Vec::new();
    for url in fetch_yage_kit_post_urls(client, 120).await? {
        let Some(d) = extract_yage_date_from_url(&url) else {
            continue;
        };
        if d < cutoff_date {
            continue;
        }
        let published_at = rfc3339_from_date(d);
        let (title, content_markdown, summary) =
            match fetch_yage_article_markdown(client, &url).await {
                Some(v) => v,
                None => (
                    yage_title_from_url(&url),
                    format!("[文章原文]({url})"),
                    format!("鸭哥 AI 每日记录 {}", d.format("%Y-%m-%d")),
                ),
            };
        items.push(Entry {
            id: 0,
            subscription_id: 0,
            guid: format!("yage-kit-daily:{}", d.format("%Y-%m-%d")),
            title,
            link: url.clone(),
            summary,
            content_markdown,
            published_at_local: to_shanghai_time(published_at.as_deref()),
            published_at,
            inserted_at: None,
            last_seen_at: None,
            sample_hits: 1,
            subscription_name: None,
        });
    }
    items.sort_by(|a, b| b.published_at.cmp(&a.published_at));
    Ok(items)
}

async fn build_yage_weekly_entries(days: i64, client: &Client) -> Result<Vec<Entry>, String> {
    let cutoff_date = (Utc::now() - Duration::days(days.max(1))).date_naive();
    let mut by_week: HashMap<(i32, u32), Vec<(NaiveDate, String)>> = HashMap::new();
    for url in fetch_yage_kit_post_urls(client, 240).await? {
        let Some(d) = extract_yage_date_from_url(&url) else {
            continue;
        };
        if d < cutoff_date {
            continue;
        }
        let iso = d.iso_week();
        by_week
            .entry((iso.year(), iso.week()))
            .or_default()
            .push((d, url));
    }

    let mut out: Vec<Entry> = Vec::new();
    for ((year, week), mut posts) in by_week {
        posts.sort_by(|a, b| b.0.cmp(&a.0));
        let Some((latest_day, latest_url)) = posts.first().cloned() else {
            continue;
        };
        let mut md = String::new();
        for (d, u) in &posts {
            md.push_str(&format!("- {} [文章原文]({})\n", d.format("%Y-%m-%d"), u));
        }
        let published_at = rfc3339_from_date(latest_day);
        out.push(Entry {
            id: 0,
            subscription_id: 0,
            guid: format!("yage-kit-weekly:{year}-W{week:02}"),
            title: format!("鸭哥 AI 周记录 {}-W{:02}", year, week),
            link: latest_url,
            summary: format!("本周共 {} 条每日记录", posts.len()),
            content_markdown: md.trim().to_string(),
            published_at_local: to_shanghai_time(published_at.as_deref()),
            published_at,
            inserted_at: None,
            last_seen_at: None,
            sample_hits: 1,
            subscription_name: None,
        });
    }
    out.sort_by(|a, b| b.published_at.cmp(&a.published_at));
    Ok(out)
}

fn normalize_feed_url(url: &str) -> String {
    if url.starts_with("http://rss.jintiankansha.me/") {
        url.replacen("http://", "https://", 1)
    } else {
        url.to_string()
    }
}

async fn refresh_one(
    st: Arc<AppState>,
    sid: i64,
    days: i64,
    sample_fetches: i64,
) -> Result<Value, String> {
    let days = days.max(1);
    let mut sample_fetches = sample_fetches.max(1);
    let cutoff = (Utc::now() - Duration::days(days)).to_rfc3339();

    let subscription = {
        let _g = st
            .db_lock
            .lock()
            .map_err(|_| "db lock failed".to_string())?;
        let c = conn(&st.db_path)?;
        let mut stmt = c
            .prepare("SELECT id,biz,name,feed_url,enabled FROM subscriptions WHERE id=?1")
            .map_err(|e| e.to_string())?;
        stmt.query_row(params![sid], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
                r.get::<_, i64>(4)?,
            ))
        })
        .map_err(|e| e.to_string())?
    };

    let (_, _biz, name, feed_url, _enabled) = subscription;
    if feed_url.contains("rss.jintiankansha.me") {
        // Upstream occasionally serves stale windows; increase sampling to improve freshness.
        sample_fetches = sample_fetches.max(8);
    }
    let started_at = now_iso();
    let run_id = {
        let _g = st
            .db_lock
            .lock()
            .map_err(|_| "db lock failed".to_string())?;
        let c = conn(&st.db_path)?;
        c.execute(
            "INSERT INTO fetch_runs (subscription_id,started_at,status,sample_fetches,items_seen,items_saved) VALUES (?1,?2,'running',?3,0,0)",
            params![sid, started_at, sample_fetches],
        )
        .map_err(|e| e.to_string())?;
        c.last_insert_rowid()
    };

    let llm = load_llm_settings_compat(&st.settings_path);
    let mut seen = 0_i64;
    let mut saved = 0_i64;
    let mut ad_skipped = 0_i64;
    let mut per_guid: HashMap<String, Entry> = HashMap::new();

    let effective_feed_url = normalize_feed_url(&feed_url);
    let is_yage_daily = is_yage_kit_daily(&effective_feed_url);
    let is_yage_weekly = is_yage_kit_weekly(&effective_feed_url);
    let is_yage_mode = is_yage_daily || is_yage_weekly;

    if is_yage_mode {
        let entries = if is_yage_daily {
            build_yage_daily_entries(days, &st.http).await?
        } else {
            build_yage_weekly_entries(days, &st.http).await?
        };
        seen = entries.len() as i64;
        for mut e in entries {
            let guid = e.guid.clone();
            e.subscription_id = sid;
            match per_guid.get_mut(&guid) {
                Some(existing) => {
                    existing.sample_hits += 1;
                    if e.content_markdown.len() > existing.content_markdown.len() {
                        *existing = Entry {
                            sample_hits: existing.sample_hits,
                            ..e
                        };
                    }
                }
                None => {
                    per_guid.insert(guid, e);
                }
            }
        }
    } else {
        for _ in 0..sample_fetches {
            let feed_text = fetch_feed_text(&st.http, &effective_feed_url).await?;
            let channel = Channel::read_from(feed_text.as_bytes()).map_err(|e| e.to_string())?;
            for item in channel.items() {
                let link = item.link().unwrap_or("").to_string();
                if link.is_empty() {
                    continue;
                }
                let guid = parse_guid(&link, item.guid().map(|g| g.value()).unwrap_or(""));
                let title = item.title().unwrap_or("Untitled").to_string();
                let raw_summary = item.description().unwrap_or("").to_string();
                let raw_content = item.content().unwrap_or("").to_string();
                let content_markdown = if raw_content.is_empty() {
                    parse_html(&raw_summary)
                } else {
                    parse_html(&raw_content)
                };
                let summary = if raw_summary.is_empty() {
                    content_markdown.chars().take(500).collect::<String>()
                } else {
                    raw_summary
                };
                let published = parse_pub_date(item.pub_date());
                if let Some(p) = &published {
                    if p < &cutoff {
                        continue;
                    }
                }
                seen += 1;
                let entry = Entry {
                    id: 0,
                    subscription_id: sid,
                    guid: guid.clone(),
                    title,
                    link,
                    summary,
                    content_markdown,
                    published_at_local: to_shanghai_time(published.as_deref()),
                    published_at: published,
                    inserted_at: None,
                    last_seen_at: None,
                    sample_hits: 1,
                    subscription_name: None,
                };
                match per_guid.get_mut(&guid) {
                    Some(existing) => {
                        existing.sample_hits += 1;
                        if entry.content_markdown.len() > existing.content_markdown.len() {
                            *existing = Entry {
                                sample_hits: existing.sample_hits,
                                ..entry
                            };
                        }
                    }
                    None => {
                        per_guid.insert(guid, entry);
                    }
                }
            }
        }
    }

    let mut candidates: Vec<Entry> = per_guid.into_values().collect();
    candidates.sort_by(|a, b| b.published_at.cmp(&a.published_at));
    let mut filtered: Vec<Entry> = Vec::with_capacity(candidates.len());
    let mut ad_guids: Vec<String> = Vec::new();
    for v in candidates {
        if is_yage_mode {
            filtered.push(v);
            continue;
        }
        let score = ad_score(&v.title, &v.summary, &v.content_markdown);
        let mut is_ad = score >= 3;
        if !is_ad && score > 0 {
            if let Some(decision) = llm_is_ad(&st.http, &llm, &v.title, &v.summary).await {
                is_ad = decision;
            } else {
                is_ad = score >= 2;
            }
        }
        if is_ad {
            ad_skipped += 1;
            ad_guids.push(v.guid.clone());
            continue;
        }
        filtered.push(v);
    }

    {
        let _g = st
            .db_lock
            .lock()
            .map_err(|_| "db lock failed".to_string())?;
        let c = conn(&st.db_path)?;
        let now = now_iso();
        for v in &filtered {
            let rows = c
                .execute(
                    "UPDATE entries SET title=?1,link=?2,summary=?3,content_markdown=?4,published_at=?5,last_seen_at=?6,sample_hits=MAX(sample_hits,?7) WHERE subscription_id=?8 AND guid=?9",
                    params![
                        v.title,
                        v.link,
                        v.summary,
                        v.content_markdown,
                        v.published_at,
                        now,
                        v.sample_hits,
                        sid,
                        v.guid
                    ],
                )
                .map_err(|e| e.to_string())?;
            if rows == 0 {
                c.execute(
                    "INSERT INTO entries (subscription_id,guid,title,link,summary,content_markdown,published_at,inserted_at,last_seen_at,sample_hits) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
                    params![
                        sid,
                        v.guid,
                        v.title,
                        v.link,
                        v.summary,
                        v.content_markdown,
                        v.published_at,
                        now,
                        now,
                        v.sample_hits
                    ],
                )
                .map_err(|e| e.to_string())?;
                saved += 1;
            }
        }
        c.execute(
            "UPDATE subscriptions SET last_refresh_at=?1,last_status='ok',last_error=NULL,updated_at=?1 WHERE id=?2",
            params![now, sid],
        )
        .map_err(|e| e.to_string())?;
        for g in ad_guids {
            let _ = c.execute(
                "DELETE FROM entries WHERE subscription_id=?1 AND guid=?2",
                params![sid, g],
            );
        }
        // Hard cap per subscription: keep latest 5 rows only.
        let _ = c.execute(
            "DELETE FROM entries
             WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY subscription_id
                               ORDER BY COALESCE(published_at, inserted_at, last_seen_at) DESC, id DESC
                           ) AS rn
                    FROM entries
                    WHERE subscription_id=?1
                ) t
                WHERE rn > 5
             )",
            params![sid],
        );
        c.execute(
            "UPDATE fetch_runs SET finished_at=?1,status='ok',items_seen=?2,items_saved=?3,note=?4 WHERE id=?5",
            params![
                now,
                seen,
                saved,
                format!("max_age_days={days};ad_skipped={ad_skipped};llm_ad={}", llm.ad_route_note()),
                run_id
            ],
        )
        .map_err(|e| e.to_string())?;
    }

    Ok(json!({
        "subscription": {"id": sid, "name": name},
        "items_seen": seen,
        "items_saved": saved,
        "ad_skipped": ad_skipped
    }))
}

async fn root() -> Html<&'static str> {
    Html(
        r#"<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>RSS Sidecar · Rust</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600;700&family=Noto+Sans+SC:wght@400;500;700&display=swap');
:root{--bg:#f3efe5;--bg2:#e6dfcf;--card:#fffdf8;--line:#d6ccb7;--text:#25231d;--muted:#726a58;--ok:#1f7a4a;--err:#b44531;--accent:#a96f2e;--shadow:0 16px 34px rgba(42,31,12,.10);--hero-bg:rgba(255,252,245,.86);--hero-border:rgba(132,110,70,.26);--btn-bg:#fffef9;--btn-shadow:0 4px 12px rgba(45,32,8,.07);--btn-shadow-hover:0 8px 18px rgba(45,32,8,.12);--btn-main-from:#f7d79f;--btn-main-to:#efc57f;--btn-main-border:#dca252;--card-border:rgba(120,100,66,.22);--panel:#faf6ec;--dash:#d9cfbc;--heading:#4d4639;--link:#2f4f7b;--input-bg:#fff;--ok-border:#95cfaf;--ok-bg:#eaf8ef;--err-border:#e3a599;--err-bg:#fff1ed;--state-running-text:#145c38;--state-running-border:#8dcaab;--state-running-bg:#e7f6ee;--state-paused-text:#5a5344;--state-paused-border:#c7bca6;--state-paused-bg:#f2ecdf}
[data-theme="dark"]{--bg:#181a1c;--bg2:#22262b;--card:#272c31;--line:#3b434c;--text:#e8edf3;--muted:#aeb8c4;--ok:#57c486;--err:#ef8f7c;--accent:#d7a15f;--shadow:0 16px 34px rgba(0,0,0,.32);--hero-bg:rgba(41,46,52,.88);--hero-border:#4a5561;--btn-bg:#30363d;--btn-shadow:0 4px 12px rgba(0,0,0,.35);--btn-shadow-hover:0 8px 18px rgba(0,0,0,.45);--btn-main-from:#5c4a2f;--btn-main-to:#6d5535;--btn-main-border:#8c6f45;--card-border:#46515d;--panel:#20262c;--dash:#3a434f;--heading:#c6d0da;--link:#8eb8ff;--input-bg:#1f252b;--ok-border:#2d7a56;--ok-bg:#1f3a2e;--err-border:#8f4b43;--err-bg:#3a2725;--state-running-text:#9de7bf;--state-running-border:#2d7a56;--state-running-bg:#1f3a2e;--state-paused-text:#d5dce4;--state-paused-border:#596678;--state-paused-bg:#2d3440}
*{box-sizing:border-box}body{margin:0;color:var(--text);font-family:'IBM Plex Sans','Noto Sans SC','PingFang SC','Microsoft Yahei',sans-serif;background:radial-gradient(1000px 480px at 10% -10%, #d3e4cf 0%, transparent 58%),radial-gradient(760px 380px at 100% 0%, #f3dfba 0%, transparent 52%),linear-gradient(160deg,var(--bg),var(--bg2));min-height:100vh}
[data-theme="dark"] body{background:radial-gradient(1000px 480px at 10% -10%, #20322b 0%, transparent 58%),radial-gradient(760px 380px at 100% 0%, #433225 0%, transparent 52%),linear-gradient(160deg,var(--bg),var(--bg2))}
.wrap{max-width:1200px;margin:20px auto;padding:0 16px 26px}
.hero{background:var(--hero-bg);border:1px solid var(--hero-border);border-radius:18px;box-shadow:var(--shadow);padding:16px 18px;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px}
.title{font-size:24px;font-weight:700;margin:0}.sub{margin:5px 0 0;color:var(--muted);font-size:13px}
.btns{display:flex;gap:10px;flex-wrap:wrap}button{border:1px solid var(--line);border-radius:11px;padding:10px 14px;cursor:pointer;font-weight:600;color:var(--text);background:var(--btn-bg);transition:.16s transform,.16s box-shadow;box-shadow:var(--btn-shadow)}button:hover{transform:translateY(-1px);box-shadow:var(--btn-shadow-hover)}.btn-main{background:linear-gradient(140deg,var(--btn-main-from),var(--btn-main-to));border-color:var(--btn-main-border)}
.theme-btn{min-width:120px}
.auto-ctl{display:inline-flex;align-items:center;gap:8px;padding:8px 10px;border:1px solid var(--line);border-radius:11px;background:var(--btn-bg);box-shadow:var(--btn-shadow);font-size:12px}
.auto-ctl input[type="number"]{width:100px;background:var(--input-bg);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:5px 6px}
.auto-ctl input[type="checkbox"]{accent-color:var(--accent)}
.auto-hint{min-width:240px}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px;margin-top:14px}.card{grid-column:span 12;background:var(--card);border:1px solid var(--card-border);border-radius:16px;box-shadow:var(--shadow);padding:14px}.h{margin:0 0 10px;font-size:17px}.muted{color:var(--muted);font-size:12px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px}.stat b{display:block;font-size:22px;line-height:1.1}
.subs .row{display:grid;grid-template-columns:1fr auto auto auto;gap:10px;align-items:center;padding:10px 0;border-bottom:1px dashed var(--dash)}.subs .row:last-child{border-bottom:none}.subs .action{display:flex;gap:6px;flex-wrap:wrap}
.state-btn{border-radius:999px;padding:6px 12px;font-size:12px;line-height:1.1;font-weight:700;box-shadow:none}
.state-btn.running{color:var(--state-running-text);border-color:var(--state-running-border);background:var(--state-running-bg)}
.state-btn.paused{color:var(--state-paused-text);border-color:var(--state-paused-border);background:var(--state-paused-bg)}
.add-sub{display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:8px;margin:0 0 10px}.add-sub input{width:100%;min-width:0;padding:9px 10px;border:1px solid var(--line);border-radius:10px;background:var(--input-bg);color:var(--text)}.add-sub button{white-space:nowrap}
.chip{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:5px 10px;font-size:12px;font-weight:600;border:1px solid}.ok{color:var(--ok);border-color:var(--ok-border);background:var(--ok-bg)}.err{color:var(--err);border-color:var(--err-border);background:var(--err-bg)}
.entries .group{margin:12px 0 16px}.entries .g-title{font-weight:700;font-size:14px;margin:0 0 8px;color:var(--heading)}
.entries article{padding:9px 0;border-bottom:1px dashed var(--dash)}.entries article:last-child{border-bottom:none}.entries a{color:var(--link);text-decoration:none;font-weight:600}.entries a:hover{text-decoration:underline}
.entry-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.entry-actions button{padding:6px 10px;border-radius:8px;font-size:12px}
.modal{position:fixed;inset:0;display:none;z-index:90}.modal.show{display:block}.modal-mask{position:absolute;inset:0;background:rgba(0,0,0,.42)}.modal-panel{position:relative;max-width:900px;max-height:85vh;overflow:auto;margin:5vh auto;background:var(--card);border:1px solid var(--card-border);border-radius:14px;box-shadow:var(--shadow);padding:14px 16px}.modal-head{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px}.modal-head h3{margin:0;font-size:18px}.md-body{line-height:1.65;color:var(--text)}.md-body img{max-width:100%;height:auto}.md-body pre{overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px}
.llm-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;align-items:end}.llm-field{min-width:0}.llm-field .muted{display:block;margin-bottom:6px}.llm-grid input{width:100%;min-width:0;padding:10px 11px;border:1px solid var(--line);border-radius:10px;background:var(--input-bg);color:var(--text)}
.llm-actions{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;align-items:center;margin-top:10px}.llm-actions button{width:100%;min-width:0}.llm-result{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;font-size:12px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px;margin-top:10px;max-height:220px;overflow:auto}
@media (min-width:980px){.stats-card{grid-column:span 4}.subs-card{grid-column:span 8}.entries-card{grid-column:span 8}.llm-card{grid-column:span 4}}
@media (max-width:760px){.subs .row{grid-template-columns:1fr auto;grid-template-areas:'name status' 'meta action'}.subs .name{grid-area:name}.subs .meta{grid-area:meta}.subs .status{grid-area:status}.subs .action{grid-area:action}.add-sub{grid-template-columns:1fr}.llm-grid{grid-template-columns:1fr}}
</style></head>
<body><div class="wrap"><section class="hero"><div><h1 class="title">RSS Sidecar · Rust</h1><p class="sub">统一订阅中台（WeChat / Yage）+ 东八区时间 + 广告文跳过（规则 + 可选LLM）</p></div><div class="btns"><button class="btn-main" onclick="refreshAll()">刷新全部订阅</button><button onclick="loadAll()">刷新页面数据</button><button id="themeToggle" class="theme-btn" onclick="toggleTheme()">Theme</button><div class="auto-ctl"><label><input id="auto_enabled" type="checkbox" onchange="saveAutoRefresh()"> 自动刷新</label><input id="auto_interval_seconds" type="number" min="5" step="1" value="3600" onchange="saveAutoRefresh()">秒<button onclick="saveAutoRefresh()">应用</button></div><div id="auto_hint" class="muted auto-hint"></div></div></section>
<section class="grid"><div class="card stats-card"><h2 class="h">运行概览</h2><div class="stats" id="stats"></div></div><div class="card subs-card"><h2 class="h">订阅列表</h2><div class="add-sub"><input id="new_biz" placeholder="biz (可选)"/><input id="new_name" placeholder="name"/><input id="new_feed_url" placeholder="feed url (https://...)"/><button onclick="createSub()">Add</button></div><div class="subs" id="subs"></div></div><div class="card entries-card"><h2 class="h">最近文章（东八区）</h2><div class="entries" id="entries"></div></div>
<div class="card llm-card"><h2 class="h">LLM 设置</h2><p class="muted">费用策略：仅允许 LongCat-Flash-Lite 自动参与广告判定；其他模型不会被 sidecar 自动调用。</p><label class="llm-switch"><input id="llm_enabled" type="checkbox"/> 启用免费 LLM 广告判定</label><div class="llm-grid"><div class="llm-field"><div class="muted">API Base</div><input id="llm_api_base" placeholder="https://api.longcat.chat/openai/v1"/></div><div class="llm-field"><div class="muted">API Key</div><input id="llm_api_key" type="password" placeholder="ak-..."/></div><div class="llm-field"><div class="muted">Model</div><input id="llm_model" placeholder="LongCat-Flash-Lite"/></div></div><div class="llm-actions"><button onclick="saveLlm()">保存设置</button><button onclick="testLlm()">测试连接</button></div><div id="llm_result" class="llm-result">这里显示模型连通测试结果。</div></div>
</section></div>
<div id="mdModal" class="modal" aria-hidden="true"><div class="modal-mask" onclick="closePreview()"></div><div class="modal-panel"><div class="modal-head"><h3 id="mdTitle">Markdown Preview</h3><button onclick="closePreview()">关闭</button></div><div id="mdBody" class="md-body muted">加载中...</div></div></div>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script>
async function j(url,opt){const r=await fetch(url,{headers:{'content-type':'application/json'},...(opt||{})});return await r.json();}
function esc(s){return (s||'').replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[m]));}
function mdToHtml(md){try{return (window.marked&&window.marked.parse)?window.marked.parse(md||''):esc(md||'').replace(/\n/g,'<br/>');}catch(_){return esc(md||'').replace(/\n/g,'<br/>');}}
function closePreview(){const m=document.getElementById('mdModal');if(!m)return;m.classList.remove('show');m.setAttribute('aria-hidden','true');}
async function openPreview(id,title){
  const m=document.getElementById('mdModal');const t=document.getElementById('mdTitle');const b=document.getElementById('mdBody');
  if(!m||!t||!b)return;
  t.textContent=title||'Markdown Preview'; b.textContent='加载中...'; m.classList.add('show'); m.setAttribute('aria-hidden','false');
  try{
    let md='';
    const r=await fetch('/api/articles/'+id+'/markdown');
    if(r.ok){ md=(await r.text())||''; }
    if(!md.trim()){
      const r2=await fetch('/api/articles/'+id);
      if(r2.ok){
        const d=await r2.json();
        const it=d.item||{};
        md=(it.content_markdown||it.summary||'').toString();
      }
    }
    b.classList.remove('muted');
    b.innerHTML=mdToHtml(md&&md.trim()?md:'(暂无可预览正文)');
  }catch(e){
    b.classList.add('muted');
    b.textContent='加载失败: '+((e&&e.message)||String(e));
  }
}
const fmtCN=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
function toCN(v){if(!v)return '';const d=new Date(v);if(Number.isNaN(d.getTime()))return v;return fmtCN.format(d);}
function ts(v){const d=new Date(v||'');return Number.isNaN(d.getTime())?0:d.getTime();}
const THEME_KEY='wechat_rss_theme';
function setThemeBtn(mode){const el=document.getElementById('themeToggle');if(!el)return;el.textContent=mode==='dark'?'Theme: Dark':'Theme: Light';}
function applyTheme(mode){document.documentElement.setAttribute('data-theme',mode);setThemeBtn(mode);}
function initTheme(){const saved=localStorage.getItem(THEME_KEY);if(saved==='dark'||saved==='light'){applyTheme(saved);return;}const prefers=window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches;applyTheme(prefers?'dark':'light');}
function toggleTheme(){const cur=document.documentElement.getAttribute('data-theme')==='dark'?'dark':'light';const next=cur==='dark'?'light':'dark';localStorage.setItem(THEME_KEY,next);applyTheme(next);}
function renderAutoHint(status){
  const el=document.getElementById('auto_hint'); if(!el) return;
  if(!status){ el.textContent='自动刷新状态未知'; return; }
  const interval=status.interval_seconds||3600;
  if(!status.enabled){ el.textContent=`自动刷新已关闭（当前间隔 ${interval} 秒）`; return; }
  const next=status.next_run_at?toCN(status.next_run_at):'待计算';
  const last=status.last_run_at?toCN(status.last_run_at):'尚未执行';
  const rs=status.last_status||'idle';
  el.textContent=`自动刷新每 ${interval} 秒 · 下次 ${next} · 上次 ${last} · 状态 ${rs}`;
}
async function loadAutoRefresh(){
  const d=await j('/api/auto-refresh-status');
  if(d.error){ renderAutoHint(null); return; }
  const enabledEl=document.getElementById('auto_enabled');
  const secEl=document.getElementById('auto_interval_seconds');
  if(enabledEl) enabledEl.checked=!!d.enabled;
  if(secEl) secEl.value=String(d.interval_seconds||3600);
  renderAutoHint(d);
}
async function saveAutoRefresh(){
  const enabled=document.getElementById('auto_enabled')?.checked!==false;
  let seconds=Number(document.getElementById('auto_interval_seconds')?.value||3600);
  if(!Number.isFinite(seconds)) seconds=3600;
  seconds=Math.max(5,Math.round(seconds));
  const d=await j('/api/settings/auto-refresh',{method:'POST',body:JSON.stringify({enabled,seconds})});
  if(d.error){ alert('保存自动刷新设置失败: '+d.error); return; }
  await loadAutoRefresh();
}
function statusChip(v){const ok=(v||'').toLowerCase()==='ok';return `<span class="chip ${ok?'ok':'err'}">${ok?'正常':'异常'} · ${esc(v||'n/a')}</span>`;}
function setLlmResult(t){document.getElementById('llm_result').textContent=t||'';}
async function loadLlm(){const d=await j('/api/settings/llm');const it=d.item||{};document.getElementById('llm_enabled').checked=!!it.enabled;document.getElementById('llm_api_base').value=it.api_base||'';document.getElementById('llm_api_key').value=it.api_key||'';document.getElementById('llm_model').value=it.model||'';setLlmResult(`策略: ${it.cost_policy||'free_only'} · 自动判定: ${it.auto_active?'开启':'关闭'} · Key: ${it.api_key_present?'已保存':'未配置'}`);}
async function saveLlm(){const p={enabled:document.getElementById('llm_enabled').checked,api_base:document.getElementById('llm_api_base').value.trim(),api_key:document.getElementById('llm_api_key').value.trim(),model:document.getElementById('llm_model').value.trim()};const d=await j('/api/settings/llm',{method:'POST',body:JSON.stringify(p)});if(d.error)throw new Error(d.error);setLlmResult('保存成功 / Saved');await loadLlm();}
async function testLlm(){const p={api_base:document.getElementById('llm_api_base').value.trim(),api_key:document.getElementById('llm_api_key').value.trim(),model:document.getElementById('llm_model').value.trim()};setLlmResult('测试中...');const d=await j('/api/settings/llm/test',{method:'POST',body:JSON.stringify(p)});if(d.error){setLlmResult('测试失败: '+d.error);return;}const it=d.item||{};setLlmResult(`连接成功
endpoint: ${it.endpoint||''}
model: ${it.model||''}
latency: ${it.latency_ms||0} ms
preview: ${it.preview||''}`);}
async function createSub(){const p={biz:document.getElementById('new_biz').value.trim(),name:document.getElementById('new_name').value.trim(),feed_url:document.getElementById('new_feed_url').value.trim()};if(!p.feed_url){alert('feed_url required');return;}const d=await j('/api/subscriptions',{method:'POST',body:JSON.stringify(p)});if(d.error){alert('create failed: '+d.error);return;}document.getElementById('new_biz').value='';document.getElementById('new_name').value='';document.getElementById('new_feed_url').value='';alert('created');await loadAll();}
async function loadStats(subs,entries){const ok=subs.filter(x=>(x.last_status||'').toLowerCase()==='ok').length;const err=subs.filter(x=>(x.last_status||'').toLowerCase()==='error').length;const enabled=subs.filter(x=>x.enabled===1).length;const stats=[['订阅总数',subs.length],['启用中',enabled],['状态正常',ok],['异常订阅',err],['最近文章',entries.length]];document.getElementById('stats').innerHTML=stats.map(s=>`<div class="stat"><span class="muted">${s[0]}</span><b>${s[1]}</b></div>`).join('');}
async function toggleSub(id){const d=await j('/api/subscriptions/'+id+'/toggle',{method:'POST',body:'{}'});if(d.error){alert('toggle failed: '+d.error);return;}await loadAll();}
async function loadSubs(){const d=await j('/api/subscriptions');const items=d.items||[];document.getElementById('subs').innerHTML=items.map(x=>`<div class="row"><div class="name"><b>${esc(x.name)}</b></div><div class="status"><button class="state-btn ${x.enabled===1?'running':'paused'}" title="${x.enabled===1?'点击暂停':'点击启动'}" onclick="toggleSub(${x.id})">${x.enabled===1?'运行中':'已暂停'}</button></div><div class="meta muted">id=${x.id} · ${statusChip(x.last_status)} · ${esc(toCN(x.last_refresh_at)||'未刷新')}</div><div class="action"><button onclick="refreshOne(${x.id})">刷新</button></div></div>`).join('');return items;}
async function refreshAll(silent){const d=await j('/api/refresh-all',{method:'POST',body:'{}'});if(!silent)alert(d.message||'done');await loadAll();}
async function loadEntries(){
  const d=await j('/api/entries?days=7&limit=40');
  const items=(d.items||[]).slice().sort((a,b)=>ts(b.published_at||b.inserted_at)-ts(a.published_at||a.inserted_at));
  const group={};
  for(const it of items){
    const k=it.subscription_name||'未命名';
    if(!group[k])group[k]=[];
    group[k].push(it);
  }
  const html=Object.keys(group).map(k=>{
    const arr=group[k].slice().sort((a,b)=>ts(b.published_at||b.inserted_at)-ts(a.published_at||a.inserted_at));
    return `<div class="group"><div class="g-title">${esc(k)}</div>${
      arr.map(x=>`<article>
        <div class="entry-actions">
          <a href="${esc(x.link)}" target="_blank" rel="noopener">${esc(x.title)}</a>
          <button onclick="openPreview(${x.id}, '${esc(x.title).replace(/'/g, '&#39;')}')">MD 预览</button>
        </div>
        <div class="muted">${esc(x.published_at_local||toCN(x.published_at)||'')}</div>
      </article>`).join('')
    }</div>`;
  }).join('');
  document.getElementById('entries').innerHTML=html||'<div class="muted">暂无文章</div>';
  return items;
}
async function refreshOne(id){const d=await j('/api/subscriptions/'+id+'/refresh',{method:'POST',body:'{}'});alert(d.message||'done');await loadAll();}
async function loadAll(){try{const [subs,entries]=await Promise.all([loadSubs(),loadEntries()]);await loadStats(subs,entries);}catch(e){document.getElementById('stats').innerHTML=`<div class="muted">加载失败: ${esc((e&&e.message)||String(e))}</div>`;}try{await loadLlm();}catch(e){setLlmResult('加载失败: '+((e&&e.message)||String(e)));}}
initTheme();
loadAutoRefresh();
setInterval(loadAutoRefresh,15000);
loadAll();
</script></body></html>"#,
    )
}

async fn health() -> Json<Value> {
    Json(json!({"ok": true, "time": now_iso()}))
}

async fn auto_refresh_status(State(st): State<Arc<AppState>>) -> Json<Value> {
    let cfg = load_auto_refresh_config(&st.settings_path);
    let runtime = match st.auto_runtime.lock() {
        Ok(g) => g.clone(),
        Err(_) => AutoRefreshRuntime::default(),
    };
    Json(json!({
        "enabled": cfg.enabled,
        "interval_seconds": cfg.interval_seconds,
        "thread_alive": runtime.thread_alive,
        "running": runtime.running,
        "last_run_at": runtime.last_run_at,
        "next_run_at": runtime.next_run_at,
        "last_status": runtime.last_status,
        "last_message": runtime.last_message
    }))
}

async fn list_subscriptions(State(st): State<Arc<AppState>>) -> Json<Value> {
    let _g = match st.db_lock.lock() {
        Ok(g) => g,
        Err(_) => return Json(json!({"error":"db lock failed","items":[] })),
    };
    let c = match conn(&st.db_path) {
        Ok(v) => v,
        Err(e) => return Json(json!({"error":e,"items":[] })),
    };
    let mut items = Vec::<Subscription>::new();
    let sql = "SELECT id,biz,name,feed_url,enabled,created_at,updated_at,last_refresh_at,last_status,last_error FROM subscriptions ORDER BY id ASC";
    if let Ok(mut stmt) = c.prepare(sql) {
        if let Ok(rows) = stmt.query_map([], |r| {
            Ok(Subscription {
                id: r.get(0)?,
                biz: r.get(1)?,
                name: r.get(2)?,
                feed_url: r.get(3)?,
                enabled: r.get(4)?,
                created_at: r.get(5)?,
                updated_at: r.get(6)?,
                last_refresh_at: r.get(7)?,
                last_status: r.get(8)?,
                last_error: r.get(9)?,
            })
        }) {
            for row in rows.flatten() {
                items.push(row);
            }
        }
    }
    Json(json!({"items": items}))
}

async fn list_entries(State(st): State<Arc<AppState>>, Query(q): Query<ListQuery>) -> Json<Value> {
    let days = q.days.unwrap_or(7).max(1);
    let limit = q.limit.unwrap_or(50).max(1);
    let cutoff = (Utc::now() - Duration::days(days)).to_rfc3339();
    let _g = match st.db_lock.lock() {
        Ok(g) => g,
        Err(_) => return Json(json!({"error":"db lock failed","items":[] })),
    };
    let c = match conn(&st.db_path) {
        Ok(v) => v,
        Err(e) => return Json(json!({"error":e,"items":[] })),
    };
    let mut items = Vec::<Entry>::new();
    let with_sid = q.subscription_id.is_some();
    let sql = if with_sid {
        "SELECT e.id,e.subscription_id,e.guid,e.title,e.link,e.summary,e.content_markdown,e.published_at,e.inserted_at,e.last_seen_at,e.sample_hits,s.name FROM entries e JOIN subscriptions s ON s.id=e.subscription_id WHERE (e.published_at IS NULL OR e.published_at >= ?1) AND e.subscription_id=?2 ORDER BY e.published_at DESC, e.inserted_at DESC LIMIT ?3"
    } else {
        "SELECT e.id,e.subscription_id,e.guid,e.title,e.link,e.summary,e.content_markdown,e.published_at,e.inserted_at,e.last_seen_at,e.sample_hits,s.name FROM entries e JOIN subscriptions s ON s.id=e.subscription_id WHERE (e.published_at IS NULL OR e.published_at >= ?1) ORDER BY e.published_at DESC, e.inserted_at DESC LIMIT ?2"
    };
    if let Ok(mut stmt) = c.prepare(sql) {
        let mapper = |r: &rusqlite::Row<'_>| {
            let published_at: Option<String> = r.get(7)?;
            Ok(Entry {
                id: r.get(0)?,
                subscription_id: r.get(1)?,
                guid: r.get(2)?,
                title: r.get(3)?,
                link: r.get(4)?,
                summary: r.get(5)?,
                content_markdown: r.get(6)?,
                published_at_local: to_shanghai_time(published_at.as_deref()),
                published_at,
                inserted_at: r.get(8)?,
                last_seen_at: r.get(9)?,
                sample_hits: r.get(10)?,
                subscription_name: r.get(11)?,
            })
        };
        let rows = if with_sid {
            stmt.query_map(
                params![cutoff, q.subscription_id.unwrap_or_default(), limit],
                mapper,
            )
        } else {
            stmt.query_map(params![cutoff, limit], mapper)
        };
        if let Ok(rows) = rows {
            for row in rows.flatten() {
                if ad_score(&row.title, &row.summary, &row.content_markdown) >= 2 {
                    continue;
                }
                items.push(row);
            }
        }
    }
    Json(json!({"items": items}))
}

async fn list_new_items(
    State(st): State<Arc<AppState>>,
    Query(q): Query<ListQuery>,
) -> Json<Value> {
    let hours = q.hours.unwrap_or(24).max(1);
    let limit = q.limit.unwrap_or(20).max(1);
    let cutoff = (Utc::now() - Duration::hours(hours)).to_rfc3339();
    let _g = match st.db_lock.lock() {
        Ok(g) => g,
        Err(_) => return Json(json!({"error":"db lock failed","items":[] })),
    };
    let c = match conn(&st.db_path) {
        Ok(v) => v,
        Err(e) => return Json(json!({"error":e,"items":[] })),
    };
    let mut items = Vec::<Entry>::new();
    let sql = "SELECT e.id,e.subscription_id,e.guid,e.title,e.link,e.summary,e.content_markdown,e.published_at,e.inserted_at,e.last_seen_at,e.sample_hits,s.name FROM entries e JOIN subscriptions s ON s.id=e.subscription_id WHERE e.inserted_at >= ?1 ORDER BY e.inserted_at DESC LIMIT ?2";
    if let Ok(mut stmt) = c.prepare(sql) {
        if let Ok(rows) = stmt.query_map(params![cutoff, limit], |r| {
            let published_at: Option<String> = r.get(7)?;
            Ok(Entry {
                id: r.get(0)?,
                subscription_id: r.get(1)?,
                guid: r.get(2)?,
                title: r.get(3)?,
                link: r.get(4)?,
                summary: r.get(5)?,
                content_markdown: r.get(6)?,
                published_at_local: to_shanghai_time(published_at.as_deref()),
                published_at,
                inserted_at: r.get(8)?,
                last_seen_at: r.get(9)?,
                sample_hits: r.get(10)?,
                subscription_name: r.get(11)?,
            })
        }) {
            for row in rows.flatten() {
                if ad_score(&row.title, &row.summary, &row.content_markdown) >= 2 {
                    continue;
                }
                items.push(row);
            }
        }
    }
    Json(json!({"items": items}))
}

async fn list_runs(State(st): State<Arc<AppState>>, Query(q): Query<ListQuery>) -> Json<Value> {
    let limit = q.limit.unwrap_or(20).max(1);
    let _g = match st.db_lock.lock() {
        Ok(g) => g,
        Err(_) => return Json(json!({"error":"db lock failed","items":[] })),
    };
    let c = match conn(&st.db_path) {
        Ok(v) => v,
        Err(e) => return Json(json!({"error":e,"items":[] })),
    };
    let with_sid = q.subscription_id.is_some();
    let sql = if with_sid {
        "SELECT id,subscription_id,started_at,finished_at,status,sample_fetches,items_seen,items_saved,note FROM fetch_runs WHERE subscription_id=?1 ORDER BY id DESC LIMIT ?2"
    } else {
        "SELECT id,subscription_id,started_at,finished_at,status,sample_fetches,items_seen,items_saved,note FROM fetch_runs ORDER BY id DESC LIMIT ?1"
    };
    let mut items = Vec::<FetchRun>::new();
    if let Ok(mut stmt) = c.prepare(sql) {
        let mapper = |r: &rusqlite::Row<'_>| {
            Ok(FetchRun {
                id: r.get(0)?,
                subscription_id: r.get(1)?,
                started_at: r.get(2)?,
                finished_at: r.get(3)?,
                status: r.get(4)?,
                sample_fetches: r.get(5)?,
                items_seen: r.get(6)?,
                items_saved: r.get(7)?,
                note: r.get(8)?,
            })
        };
        let rows = if with_sid {
            stmt.query_map(
                params![q.subscription_id.unwrap_or_default(), limit],
                mapper,
            )
        } else {
            stmt.query_map(params![limit], mapper)
        };
        if let Ok(rows) = rows {
            for row in rows.flatten() {
                items.push(row);
            }
        }
    }
    Json(json!({"items": items}))
}

async fn get_settings_llm(State(st): State<Arc<AppState>>) -> Json<Value> {
    let llm = load_llm_settings_compat(&st.settings_path);
    Json(json!({"item": llm.public_json()}))
}

async fn get_article(State(st): State<Arc<AppState>>, Path(id): Path<i64>) -> Json<Value> {
    let _g = match st.db_lock.lock() {
        Ok(g) => g,
        Err(_) => return Json(json!({"error":"db lock failed"})),
    };
    let c = match conn(&st.db_path) {
        Ok(v) => v,
        Err(e) => return Json(json!({"error":e})),
    };
    let sql = "SELECT e.id,e.subscription_id,e.guid,e.title,e.link,e.summary,e.content_markdown,e.published_at,e.inserted_at,e.last_seen_at,e.sample_hits,s.name FROM entries e JOIN subscriptions s ON s.id=e.subscription_id WHERE e.id=?1";
    let mut stmt = match c.prepare(sql) {
        Ok(v) => v,
        Err(e) => return Json(json!({"error":e.to_string()})),
    };
    let row = stmt.query_row(params![id], |r| {
        let published_at: Option<String> = r.get(7)?;
        Ok(Entry {
            id: r.get(0)?,
            subscription_id: r.get(1)?,
            guid: r.get(2)?,
            title: r.get(3)?,
            link: r.get(4)?,
            summary: r.get(5)?,
            content_markdown: r.get(6)?,
            published_at_local: to_shanghai_time(published_at.as_deref()),
            published_at,
            inserted_at: r.get(8)?,
            last_seen_at: r.get(9)?,
            sample_hits: r.get(10)?,
            subscription_name: r.get(11)?,
        })
    });
    match row {
        Ok(v) => Json(
            json!({"item": { "id": v.id, "title": v.title, "link": v.link, "summary": v.summary, "content_markdown": v.content_markdown, "published_at": v.published_at, "published_at_local": v.published_at_local, "inserted_at": v.inserted_at, "subscription_name": v.subscription_name, "article_markdown": if v.content_markdown.is_empty() { v.summary } else { v.content_markdown } }}),
        ),
        Err(_) => Json(json!({"error":"entry not found"})),
    }
}

async fn get_article_markdown(
    State(st): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> impl IntoResponse {
    let Json(v) = get_article(State(st.clone()), Path(id)).await;
    if let Some(err) = v.get("error") {
        return (StatusCode::NOT_FOUND, err.to_string()).into_response();
    }
    let mut md = v
        .get("item")
        .and_then(|x| x.get("article_markdown"))
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string();
    let link = v
        .get("item")
        .and_then(|x| x.get("link"))
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string();
    let summary = v
        .get("item")
        .and_then(|x| x.get("summary"))
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string();

    if md.trim().len() < 240 {
        if let Some(fetched) = fetch_article_markdown_from_link(&st.http, &link).await {
            if fetched.len() > md.len() {
                md = fetched.clone();
            }
            if let Ok(_g) = st.db_lock.lock() {
                if let Ok(c) = conn(&st.db_path) {
                    let min_len = md.len() as i64;
                    let _ = c.execute(
                        "UPDATE entries SET content_markdown=CASE WHEN length(coalesce(content_markdown,''))>=?1 THEN content_markdown ELSE ?2 END WHERE id=?3",
                        params![min_len, md.clone(), id],
                    );
                }
            }
        } else if md.trim().is_empty() {
            md = summary;
        }
    }
    if md.trim().is_empty() {
        md = "(暂无可预览正文)".to_string();
    }
    ([(header::CONTENT_TYPE, "text/plain; charset=utf-8")], md).into_response()
}

async fn create_subscription(
    State(st): State<Arc<AppState>>,
    Json(payload): Json<Value>,
) -> Json<Value> {
    let mut biz = payload
        .get("biz")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    let name = payload
        .get("name")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    let feed_url = payload
        .get("feed_url")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    if feed_url.is_empty() {
        return Json(json!({"error":"feed_url required"}));
    }
    if biz.is_empty() {
        if let Some(v) = query_param(&feed_url, "__biz") {
            biz = v;
        }
    }
    if biz.is_empty() {
        let host = feed_url
            .split('/')
            .nth(2)
            .unwrap_or("unknown")
            .to_lowercase();
        let tail = feed_url
            .rsplit('/')
            .next()
            .unwrap_or("unknown")
            .chars()
            .take(24)
            .collect::<String>();
        biz = format!("custom:{host}:{tail}");
    }
    let now = now_iso();
    let _g = match st.db_lock.lock() {
        Ok(g) => g,
        Err(_) => return Json(json!({"error":"db lock failed"})),
    };
    let c = match conn(&st.db_path) {
        Ok(v) => v,
        Err(e) => return Json(json!({"error":e})),
    };
    let _ = c.execute(
        "INSERT INTO subscriptions (biz,name,feed_url,enabled,created_at,updated_at) VALUES (?1,?2,?3,1,?4,?4)
         ON CONFLICT(biz) DO UPDATE SET name=excluded.name, feed_url=excluded.feed_url, updated_at=excluded.updated_at",
        params![biz, if name.is_empty() { "Unnamed".to_string() } else { name }, feed_url, now],
    );
    Json(json!({"message":"Saved"}))
}

async fn toggle_subscription(State(st): State<Arc<AppState>>, Path(id): Path<i64>) -> Json<Value> {
    let _g = match st.db_lock.lock() {
        Ok(g) => g,
        Err(_) => return Json(json!({"error":"db lock failed"})),
    };
    let c = match conn(&st.db_path) {
        Ok(v) => v,
        Err(e) => return Json(json!({"error":e})),
    };
    let enabled: i64 = c
        .query_row(
            "SELECT enabled FROM subscriptions WHERE id=?1",
            params![id],
            |r| r.get(0),
        )
        .unwrap_or(1);
    let new_v = if enabled == 0 { 1 } else { 0 };
    let _ = c.execute(
        "UPDATE subscriptions SET enabled=?1,updated_at=?2 WHERE id=?3",
        params![new_v, now_iso(), id],
    );
    Json(json!({"message":"OK","item":{"id":id,"enabled":new_v}}))
}

async fn update_subscription(
    State(st): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Json(payload): Json<Value>,
) -> Json<Value> {
    let name = payload
        .get("name")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    let feed_url = payload
        .get("feed_url")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    let _g = match st.db_lock.lock() {
        Ok(g) => g,
        Err(_) => return Json(json!({"error":"db lock failed"})),
    };
    let c = match conn(&st.db_path) {
        Ok(v) => v,
        Err(e) => return Json(json!({"error":e})),
    };
    let _ = c.execute(
        "UPDATE subscriptions SET name=COALESCE(NULLIF(?1,''),name),feed_url=COALESCE(NULLIF(?2,''),feed_url),updated_at=?3 WHERE id=?4",
        params![name, feed_url, now_iso(), id],
    );
    Json(json!({"message":"Updated"}))
}

async fn delete_subscription(State(st): State<Arc<AppState>>, Path(id): Path<i64>) -> Json<Value> {
    let _g = match st.db_lock.lock() {
        Ok(g) => g,
        Err(_) => return Json(json!({"error":"db lock failed"})),
    };
    let c = match conn(&st.db_path) {
        Ok(v) => v,
        Err(e) => return Json(json!({"error":e})),
    };
    let _ = c.execute("DELETE FROM entries WHERE subscription_id=?1", params![id]);
    let _ = c.execute(
        "DELETE FROM fetch_runs WHERE subscription_id=?1",
        params![id],
    );
    let _ = c.execute("DELETE FROM subscriptions WHERE id=?1", params![id]);
    Json(json!({"message":"Deleted"}))
}

async fn set_auto_refresh(
    State(st): State<Arc<AppState>>,
    Json(payload): Json<Value>,
) -> Json<Value> {
    let enabled = payload
        .get("enabled")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    let seconds = payload
        .get("seconds")
        .and_then(|v| v.as_i64())
        .or_else(|| payload.get("interval_seconds").and_then(|v| v.as_i64()))
        .or_else(|| {
            payload
                .get("minutes")
                .and_then(|v| v.as_i64())
                .map(|v| v * 60)
        })
        .unwrap_or(3600)
        .clamp(5, 86400);
    let mut settings = read_settings(&st.settings_path);
    settings["auto_refresh_enabled"] = json!(enabled);
    settings["auto_refresh_seconds"] = json!(seconds);
    settings["auto_refresh_minutes"] = json!((seconds / 60).max(1));
    if let Err(e) = write_settings(&st.settings_path, &settings) {
        return Json(json!({"error":e}));
    }
    Json(json!({
        "message": format!("auto refresh: {} / {}s", if enabled { "enabled" } else { "disabled" }, seconds),
        "enabled": enabled,
        "interval_seconds": seconds
    }))
}

async fn set_llm_settings(
    State(st): State<Arc<AppState>>,
    Json(payload): Json<Value>,
) -> Json<Value> {
    let llm = load_llm_settings_compat(&st.settings_path).with_payload(&payload, true);
    let mut settings = read_settings(&st.settings_path);
    settings["llm_enabled"] = json!(llm.enabled);
    settings["api_base"] = json!(llm.api_base.clone());
    settings["api_key"] = json!(llm.api_key.clone());
    settings["model"] = json!(llm.model.clone());
    settings["llm"] = llm.stored_json();
    if let Err(e) = write_settings(&st.settings_path, &settings) {
        return Json(json!({"error":e}));
    }
    Json(json!({"message":"LLM settings saved","item": llm.public_json()}))
}

async fn test_llm_settings(
    State(st): State<Arc<AppState>>,
    Json(payload): Json<Value>,
) -> Json<Value> {
    let llm = load_llm_settings_compat(&st.settings_path).with_payload(&payload, true);
    if !llm.configured() {
        return Json(json!({"error":"Please provide API Base, API Key, and Model"}));
    }
    if !llm.free_allowed() {
        return Json(json!({"error": FREE_LLM_ERROR}));
    }
    let url = llm.chat_completions_url();
    let body = json!({
        "model": llm.model,
        "messages":[{"role":"user","content":"Reply with OK only."}],
        "max_tokens": 8,
        "temperature": 0
    });
    let started = std::time::Instant::now();
    let resp = st
        .http
        .post(url.clone())
        .bearer_auth(llm.api_key.clone())
        .json(&body)
        .timeout(std::time::Duration::from_secs(30))
        .send()
        .await;
    let Ok(resp) = resp else {
        return Json(json!({"error":"Connection failed"}));
    };
    let status = resp.status().as_u16();
    let text = resp.text().await.unwrap_or_default();
    if status >= 400 {
        return Json(
            json!({"error": format!("HTTP {}: {}", status, text.chars().take(300).collect::<String>())}),
        );
    }
    let mut preview = String::new();
    if let Ok(parsed) = serde_json::from_str::<Value>(&text) {
        preview = parsed
            .get("choices")
            .and_then(|v| v.as_array())
            .and_then(|arr| arr.first())
            .and_then(|x| x.get("message"))
            .and_then(|x| x.get("content"))
            .and_then(|x| x.as_str())
            .unwrap_or("")
            .to_string();
    }
    Json(json!({
        "message":"Connection OK",
        "item":{
            "ok": true,
            "status_code": status,
            "latency_ms": started.elapsed().as_millis() as i64,
            "preview": preview.chars().take(160).collect::<String>(),
            "endpoint": url,
            "model": llm.model
        }
    }))
}

async fn refresh_all_impl(st: Arc<AppState>, days: i64, sample_fetches: i64) -> Value {
    let subs = {
        let _g = match st.db_lock.lock() {
            Ok(g) => g,
            Err(_) => return json!({"error":"db lock failed","items":[] }),
        };
        let c = match conn(&st.db_path) {
            Ok(v) => v,
            Err(e) => return json!({"error":e,"items":[] }),
        };
        let mut ids = Vec::new();
        if let Ok(mut stmt) =
            c.prepare("SELECT id,name FROM subscriptions WHERE enabled=1 ORDER BY id ASC")
        {
            if let Ok(rows) =
                stmt.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))
            {
                for row in rows.flatten() {
                    ids.push(row);
                }
            }
        }
        ids
    };
    let mut items = Vec::<Value>::new();
    for (sid, name) in subs {
        match refresh_one(st.clone(), sid, days, sample_fetches).await {
            Ok(v) => items.push(json!({"id":sid,"name":name,"status":"ok","items_seen":v["items_seen"],"items_saved":v["items_saved"]})),
            Err(e) => items.push(json!({"id":sid,"name":name,"status":"error","error":e})),
        }
    }
    json!({"message": format!("Refreshed {} subscriptions", items.len()), "items": items})
}

async fn refresh_all(
    State(st): State<Arc<AppState>>,
    Json(payload): Json<RefreshPayload>,
) -> Json<Value> {
    let days = payload.days.unwrap_or(7);
    let sample_fetches = payload.sample_fetches.unwrap_or(3);
    let _sample_interval = payload.sample_interval.unwrap_or(1.0);
    Json(refresh_all_impl(st, days, sample_fetches).await)
}

async fn refresh_subscription(
    State(st): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Json(payload): Json<RefreshPayload>,
) -> Json<Value> {
    let days = payload.days.unwrap_or(7);
    let sample_fetches = payload.sample_fetches.unwrap_or(3);
    let _sample_interval = payload.sample_interval.unwrap_or(1.0);
    match refresh_one(st, id, days, sample_fetches).await {
        Ok(v) => Json(json!({
            "message": format!("Refreshed subscription {id} ({} seen, {} saved)", v["items_seen"], v["items_saved"]),
            "subscription": v["subscription"],
            "items_seen": v["items_seen"],
            "items_saved": v["items_saved"]
        })),
        Err(e) => Json(json!({"error":e})),
    }
}

async fn auto_refresh_loop(st: Arc<AppState>) {
    let mut next_due: Option<DateTime<Utc>> = None;
    let mut last_cfg = AutoRefreshConfig::default();
    loop {
        let cfg = load_auto_refresh_config(&st.settings_path);
        let now = Utc::now();

        {
            if let Ok(mut rt) = st.auto_runtime.lock() {
                rt.thread_alive = true;
                if !cfg.enabled {
                    rt.running = false;
                    rt.next_run_at = None;
                    if rt.last_status == "idle" {
                        rt.last_status = "disabled".to_string();
                    }
                }
            }
        }

        if !cfg.enabled {
            next_due = None;
            last_cfg = cfg;
            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
            continue;
        }

        if next_due.is_none()
            || cfg.interval_seconds != last_cfg.interval_seconds
            || cfg.enabled != last_cfg.enabled
        {
            next_due = Some(now + Duration::seconds(cfg.interval_seconds));
        }
        last_cfg = cfg.clone();

        if let Some(due) = next_due {
            {
                if let Ok(mut rt) = st.auto_runtime.lock() {
                    rt.next_run_at = Some(due.to_rfc3339());
                }
            }
            if now >= due {
                {
                    if let Ok(mut rt) = st.auto_runtime.lock() {
                        rt.running = true;
                        rt.last_status = "running".to_string();
                    }
                }
                let result = refresh_all_impl(st.clone(), 7, 3).await;
                let items = result
                    .get("items")
                    .and_then(|v| v.as_array())
                    .cloned()
                    .unwrap_or_default();
                let errors = items
                    .iter()
                    .filter(|x| x.get("status").and_then(|s| s.as_str()) == Some("error"))
                    .count();
                let status = if errors == 0 { "ok" } else { "partial_error" };
                let message = result
                    .get("message")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                {
                    if let Ok(mut rt) = st.auto_runtime.lock() {
                        rt.running = false;
                        rt.last_run_at = Some(Utc::now().to_rfc3339());
                        rt.last_status = status.to_string();
                        rt.last_message = if errors == 0 {
                            message
                        } else {
                            format!("{message}; errors={errors}")
                        };
                    }
                }
                next_due = Some(Utc::now() + Duration::seconds(cfg.interval_seconds));
            }
        }
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
    }
}

#[tokio::main]
async fn main() {
    let host = std::env::var("WECHAT_RSS_HOST").unwrap_or_else(|_| "0.0.0.0".to_string());
    let port: u16 = std::env::var("WECHAT_RSS_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8091);
    let base_dir = std::env::var("WECHAT_RSS_BASE_DIR")
        .unwrap_or_else(|_| "/root/.nanobot/workspace/wechat_rss_service".to_string());
    let db_path =
        std::env::var("WECHAT_RSS_DB").unwrap_or_else(|_| format!("{base_dir}/service.db"));
    let settings_path = std::env::var("WECHAT_RSS_SETTINGS")
        .unwrap_or_else(|_| format!("{base_dir}/settings.json"));

    let _ = db::init_db(&db_path);
    let http = Client::builder()
        .timeout(std::time::Duration::from_secs(25))
        .user_agent("wechat-rss-rs/0.2")
        .build()
        .expect("http client build failed");
    let state = Arc::new(AppState {
        db_path: PathBuf::from(db_path),
        settings_path: PathBuf::from(settings_path),
        db_lock: Arc::new(Mutex::new(())),
        http,
        auto_runtime: Arc::new(Mutex::new(AutoRefreshRuntime::default())),
    });

    tokio::spawn(auto_refresh_loop(state.clone()));

    let app = Router::new()
        .route("/", get(root))
        .route("/api/health", get(health))
        .route("/api/auto-refresh-status", get(auto_refresh_status))
        .route(
            "/api/subscriptions",
            get(list_subscriptions).post(create_subscription),
        )
        .route("/api/subscriptions/{id}/toggle", post(toggle_subscription))
        .route("/api/subscriptions/{id}/update", post(update_subscription))
        .route("/api/subscriptions/{id}/delete", post(delete_subscription))
        .route(
            "/api/subscriptions/{id}/refresh",
            post(refresh_subscription),
        )
        .route("/api/entries", get(list_entries))
        .route("/api/timeline", get(list_entries))
        .route("/api/new-items", get(list_new_items))
        .route("/api/runs", get(list_runs))
        .route(
            "/api/settings/llm",
            get(get_settings_llm).post(set_llm_settings),
        )
        .route("/api/settings/llm/test", post(test_llm_settings))
        .route("/api/settings/auto-refresh", post(set_auto_refresh))
        .route("/api/articles/{id}", get(get_article))
        .route("/api/articles/{id}/markdown", get(get_article_markdown))
        .route("/api/refresh-all", post(refresh_all))
        .with_state(state);

    let addr: SocketAddr = format!("{}:{}", host, port)
        .parse()
        .expect("invalid address");
    println!("wechat-rss-rs listening on http://{}", addr);
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("bind failed");
    axum::serve(listener, app).await.expect("server failed");
}
