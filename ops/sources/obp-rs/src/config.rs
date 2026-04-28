use serde::{Deserialize, Serialize};
use std::fs;
use std::path::Path;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Channel {
    pub id: Option<u64>,
    pub name: String,
    pub r#type: String,
    pub key: String,
    pub base: String,
    #[serde(default = "default_models")]
    pub models: String,
    #[serde(default)]
    pub model_mapping: String,
    pub status: String,
    pub requests: u64,
    pub last_test: Option<String>,
    pub fail_count: u32,
}

fn default_models() -> String {
    "*".to_string()
}

pub fn load_config<P: AsRef<Path>>(path: P) -> Vec<Channel> {
    if !path.as_ref().exists() {
        return Vec::new();
    }
    let data = fs::read_to_string(path).unwrap_or_default();
    serde_json::from_str(&data).unwrap_or_default()
}

pub fn save_config<P: AsRef<Path>>(path: P, channels: &[Channel]) {
    let data = serde_json::to_string_pretty(channels).unwrap_or_default();
    let _ = fs::write(path, data);
}
