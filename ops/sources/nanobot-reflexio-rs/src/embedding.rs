use crate::cost_policy::{embedding_enabled, FREE_EMBEDDING_ENDPOINT, FREE_EMBEDDING_MODEL};
use anyhow::Result;
use reqwest::Client;
use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize)]
struct EmbeddingRequest {
    model: String,
    input: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct EmbeddingResponse {
    data: Vec<EmbeddingData>,
}

#[derive(Debug, Deserialize)]
struct EmbeddingData {
    embedding: Vec<f32>,
}

pub struct EmbeddingService {
    client: Client,
    api_key: String,
    endpoint: String,
    model: String,
    enabled: bool,
}

impl EmbeddingService {
    pub fn new(api_key: String) -> Self {
        let endpoint = std::env::var("EMBEDDING_ENDPOINT")
            .unwrap_or_else(|_| FREE_EMBEDDING_ENDPOINT.to_string());
        let model =
            std::env::var("EMBEDDING_MODEL").unwrap_or_else(|_| FREE_EMBEDDING_MODEL.to_string());
        let enabled = embedding_enabled(&api_key, &endpoint, &model);
        Self {
            client: Client::new(),
            api_key,
            endpoint,
            model,
            enabled,
        }
    }

    pub fn enabled(&self) -> bool {
        self.enabled
    }

    pub async fn embed(&self, texts: &[String]) -> Result<Vec<Vec<f32>>> {
        if texts.is_empty() {
            return Ok(vec![]);
        }
        if !self.enabled {
            anyhow::bail!("embedding disabled by free-only sidecar policy");
        }
        let req = EmbeddingRequest {
            model: self.model.clone(),
            input: texts.to_vec(),
        };
        let resp = self
            .client
            .post(&self.endpoint)
            .header("Authorization", format!("Bearer {}", self.api_key))
            .json(&req)
            .timeout(std::time::Duration::from_secs(30))
            .send()
            .await?;

        if !resp.status().is_success() {
            let err = resp.text().await?;
            anyhow::bail!("Embedding API error: {}", err);
        }

        let result: EmbeddingResponse = resp.json().await?;
        Ok(result.data.into_iter().map(|d| d.embedding).collect())
    }

    pub async fn embed_single(&self, text: &str) -> Result<Vec<f32>> {
        let vecs = self.embed(&[text.to_string()]).await?;
        Ok(vecs.into_iter().next().unwrap_or_default())
    }
}

pub fn cosine_similarity(a: &[f32], b: &[f32]) -> f32 {
    if a.len() != b.len() || a.is_empty() {
        return 0.0;
    }
    let dot: f32 = a.iter().zip(b.iter()).map(|(x, y)| x * y).sum();
    let norm_a: f32 = a.iter().map(|x| x * x).sum::<f32>().sqrt();
    let norm_b: f32 = b.iter().map(|x| x * x).sum::<f32>().sqrt();
    if norm_a == 0.0 || norm_b == 0.0 {
        return 0.0;
    }
    dot / norm_a / norm_b
}

pub fn vec_to_bytes(vec: &[f32]) -> Vec<u8> {
    vec.iter().flat_map(|f| f.to_le_bytes()).collect()
}

pub fn bytes_to_vec(bytes: &[u8]) -> Vec<f32> {
    bytes
        .chunks_exact(4)
        .map(|chunk| f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]))
        .collect()
}

#[derive(Debug, Serialize)]
pub struct SearchResult {
    pub id: i64,
    pub score: f32,
    pub content: String,
    pub kind: String,
    pub created_at: String,
}
