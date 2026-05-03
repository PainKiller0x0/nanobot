mod embedding;
mod provider;
mod reasoning;
mod storage;

use axum::{
    extract::{Query, State},
    http::StatusCode,
    response::Html,
    routing::{get, post},
    Json, Router,
};
use dotenvy::dotenv;
use serde::{Deserialize, Serialize};
use std::env;
use std::net::SocketAddr;
use std::path::Path;
use std::sync::{Arc, Mutex};

use crate::embedding::{EmbeddingService, SearchResult};
use crate::provider::{ChatMessage, LlmProvider};
use crate::storage::DbStore;

struct AppState {
    provider: LlmProvider,
    embedding: EmbeddingService,
    db: Mutex<DbStore>,
}

#[derive(Debug, Deserialize)]
struct InteractionData {
    role: String,
    content: String,
}

#[derive(Debug, Deserialize)]
struct PublishInteractionRequest {
    user_id: String,
    interaction_data_list: Vec<InteractionData>,
    #[serde(default)]
    session_id: Option<String>,
}

#[derive(Debug, Serialize)]
struct GenericResponse {
    success: bool,
    msg: String,
}

#[derive(Debug, Serialize)]
struct HealthResponse {
    status: String,
    version: String,
}

#[derive(Debug, Serialize)]
struct StatsResponse {
    total_interactions: i64,
    total_facts: i64,
    total_memories: i64,
    latest_memory_at: Option<String>,
}

#[derive(Debug, Deserialize)]
struct MemoryListQuery {
    limit: Option<usize>,
}

#[derive(Debug, Deserialize)]
struct MemoryWriteRequest {
    content: String,
    user_id: Option<String>,
    category: Option<String>,
    source: Option<String>,
}

#[derive(Debug, Serialize)]
struct MemoryWriteResponse {
    success: bool,
    msg: String,
    id: Option<i64>,
}

#[derive(Debug, Deserialize)]
struct MemorySearchRequest {
    query: String,
    limit: Option<usize>,
}

#[derive(Debug, Serialize)]
struct MemorySearchResponse {
    results: Vec<storage::MemoryRecord>,
}

#[derive(Debug, Deserialize)]
struct SearchRequest {
    query: String,
    #[serde(default = "default_limit")]
    limit: usize,
    #[serde(default = "default_threshold")]
    threshold: f32,
}

fn default_limit() -> usize {
    5
}
fn default_threshold() -> f32 {
    0.3
}

#[derive(Debug, Serialize)]
struct SearchResponse {
    results: Vec<SearchResult>,
}

const DASHBOARD_HTML: &str = include_str!("dashboard.html");

#[tokio::main]
async fn main() {
    dotenv().ok();
    tracing_subscriber::fmt::init();

    let api_key = env::var("LLM_API_KEY").expect("LLM_API_KEY must be set");
    let base_url =
        env::var("LLM_BASE_URL").unwrap_or_else(|_| "https://api.openai.com/v1".to_string());
    let db_path = env::var("DATABASE_URL").unwrap_or_else(|_| "reflexio.db".to_string());
    if let Some(parent) = Path::new(&db_path).parent() {
        let _ = std::fs::create_dir_all(parent);
    }

    let embed_api_key = env::var("EMBEDDING_API_KEY")
        .unwrap_or_else(|_| env::var("SILICONFLOW_API_KEY").unwrap_or_else(|_| api_key.clone()));

    let state = Arc::new(AppState {
        provider: LlmProvider::new(api_key, base_url),
        embedding: EmbeddingService::new(embed_api_key),
        db: Mutex::new(DbStore::new(&db_path).expect("Failed to init DB")),
    });

    let app = Router::new()
        .route("/", get(dashboard))
        .route("/health", get(health))
        .route("/api/stats", get(stats))
        .route("/api/interactions", get(list_interactions))
        .route("/api/facts", get(list_facts))
        .route("/api/memories", get(list_memories).post(add_memory))
        .route("/api/memory/search", post(search_memory))
        .route("/api/search", post(search))
        .route("/api/publish_interaction", post(publish_interaction))
        .with_state(state);

    let host = env::var("REFLEXIO_HOST").unwrap_or_else(|_| "0.0.0.0".to_string());
    let port: u16 = env::var("REFLEXIO_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8081);
    let addr: SocketAddr = format!("{}:{}", host, port).parse().unwrap();
    println!("Reflexio-RS listening on {}", addr);

    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}

async fn dashboard() -> Html<&'static str> {
    Html(DASHBOARD_HTML)
}

async fn health() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "healthy".to_string(),
        version: env!("CARGO_PKG_VERSION").to_string(),
    })
}

async fn stats(State(state): State<Arc<AppState>>) -> Result<Json<StatsResponse>, StatusCode> {
    let db = state
        .db
        .lock()
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    let total_interactions = db.count_interactions().unwrap_or(0);
    let total_facts = db.count_facts().unwrap_or(0);
    let total_memories = db.count_memories().unwrap_or(0);
    let latest_memory_at = db.latest_memory_at().unwrap_or(None);
    Ok(Json(StatsResponse {
        total_interactions,
        total_facts,
        total_memories,
        latest_memory_at,
    }))
}

async fn list_interactions(
    State(state): State<Arc<AppState>>,
) -> Result<Json<Vec<storage::InteractionRecord>>, StatusCode> {
    let db = state
        .db
        .lock()
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    let records = db.get_recent_interactions(100).unwrap_or_default();
    Ok(Json(records))
}

async fn list_facts(
    State(state): State<Arc<AppState>>,
) -> Result<Json<Vec<storage::FactRecord>>, StatusCode> {
    let db = state
        .db
        .lock()
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    let records = db.get_recent_facts(100).unwrap_or_default();
    Ok(Json(records))
}

async fn list_memories(
    State(state): State<Arc<AppState>>,
    Query(query): Query<MemoryListQuery>,
) -> Result<Json<Vec<storage::MemoryRecord>>, StatusCode> {
    let db = state
        .db
        .lock()
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    let records = db
        .get_recent_memories(query.limit.unwrap_or(100).min(500))
        .unwrap_or_default();
    Ok(Json(records))
}

async fn add_memory(
    State(state): State<Arc<AppState>>,
    Json(payload): Json<MemoryWriteRequest>,
) -> Result<Json<MemoryWriteResponse>, StatusCode> {
    let content = payload.content.trim();
    if content.is_empty() {
        return Ok(Json(MemoryWriteResponse {
            success: false,
            msg: "content is empty".to_string(),
            id: None,
        }));
    }
    let user_id = clean_field(payload.user_id.as_deref(), "default_user");
    let category = clean_field(payload.category.as_deref(), "note");
    let source = clean_field(payload.source.as_deref(), "manual");
    let content = trim_chars(content, 4000);
    let db = state
        .db
        .lock()
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    let id = db
        .save_memory(&user_id, &category, &content, &source)
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    Ok(Json(MemoryWriteResponse {
        success: true,
        msg: "remembered locally".to_string(),
        id: Some(id),
    }))
}

async fn search_memory(
    State(state): State<Arc<AppState>>,
    Json(payload): Json<MemorySearchRequest>,
) -> Result<Json<MemorySearchResponse>, StatusCode> {
    let db = state
        .db
        .lock()
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    let results = db
        .search_memories(&payload.query, payload.limit.unwrap_or(8).min(50))
        .unwrap_or_default();
    Ok(Json(MemorySearchResponse { results }))
}

fn clean_field(value: Option<&str>, default: &str) -> String {
    let value = value.unwrap_or(default).trim();
    let value = if value.is_empty() { default } else { value };
    trim_chars(value, 80)
}

fn trim_chars(value: &str, limit: usize) -> String {
    value.chars().take(limit).collect()
}

async fn search(
    State(state): State<Arc<AppState>>,
    Json(payload): Json<SearchRequest>,
) -> Result<Json<SearchResponse>, StatusCode> {
    if payload.query.is_empty() {
        return Ok(Json(SearchResponse { results: vec![] }));
    }

    let query_emb = state
        .embedding
        .embed_single(&payload.query)
        .await
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

    let db = state
        .db
        .lock()
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    let results = db
        .search_similar(&query_emb, payload.limit, payload.threshold)
        .unwrap_or_default();

    Ok(Json(SearchResponse { results }))
}

async fn publish_interaction(
    State(state): State<Arc<AppState>>,
    Json(payload): Json<PublishInteractionRequest>,
) -> Json<GenericResponse> {
    let content = payload
        .interaction_data_list
        .first()
        .map(|i| i.content.clone())
        .unwrap_or_default();

    if content.is_empty() {
        return Json(GenericResponse {
            success: true,
            msg: "Empty".to_string(),
        });
    }

    // Save interaction
    let int_id = {
        let db = state.db.lock().unwrap();
        db.save_interaction(&payload.user_id, payload.session_id.as_deref(), &content)
            .unwrap_or(-1)
    };

    // Embed interaction content (fire-and-forget)
    let state_clone = Arc::clone(&state);
    let content_for_embed = content.clone();
    tokio::spawn(async move {
        if let Ok(emb) = state_clone.embedding.embed_single(&content_for_embed).await {
            let db = state_clone.db.lock().unwrap();
            let _ = db.update_interaction_embedding(int_id, &emb);
        }
    });

    // Extract facts via LLM (fire-and-forget)
    let state_clone2 = Arc::clone(&state);
    let user_id = payload.user_id.clone();
    tokio::spawn(async move {
        let messages = vec![
            ChatMessage {
                role: "system".to_string(),
                content: crate::reasoning::PROFILE_UPDATE_PROMPT.to_string(),
            },
            ChatMessage {
                role: "user".to_string(),
                content: content.clone(),
            },
        ];
        if let Ok(reflection) = state_clone2.provider.query("minimax-m2.7", messages).await {
            if reflection.len() > 5 && !reflection.contains("PASS") {
                let fact_id = {
                    let db = state_clone2.db.lock().unwrap();
                    db.save_fact(&user_id, &reflection).unwrap_or(-1)
                };
                // Embed the fact
                if let Ok(emb) = state_clone2.embedding.embed_single(&reflection).await {
                    let db = state_clone2.db.lock().unwrap();
                    let _ = db.update_fact_embedding(fact_id, &emb);
                }
                println!("Fact extracted for {}: {} bytes", user_id, reflection.len());
            }
        }
    });

    Json(GenericResponse {
        success: true,
        msg: "Accepted".to_string(),
    })
}
