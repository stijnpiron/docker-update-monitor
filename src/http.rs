use std::time::Duration;

/// Build a shared HTTP client with a 30-second timeout.
/// Retry logic (3 attempts on 429/5xx, Retry-After) is applied per-call in the scanner.
pub fn build_client() -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()
        .expect("failed to build HTTP client")
}
