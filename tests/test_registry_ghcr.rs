use docker_update_monitor::registry::ghcr::fetch_ghcr_tags;
use wiremock::matchers::{method, path, query_param};
use wiremock::{Mock, MockServer, ResponseTemplate};

#[tokio::test]
async fn test_no_github_token_returns_empty() {
    let client = reqwest::Client::new();
    let tags = fetch_ghcr_tags(&client, "ghcr.io/owner/repo", "", None, "http://unused").await;
    assert!(tags.is_empty());
}

#[tokio::test]
async fn test_fetch_tags_successful() {
    let server = MockServer::start().await;

    Mock::given(method("GET"))
        .and(path("/orgs/owner/packages/container/repo/versions"))
        .and(query_param("page", "1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!([
            {"metadata": {"container": {"tags": ["v1.0.0", "v1.1.0"]}}},
            {"metadata": {"container": {"tags": ["v0.9.0"]}}}
        ])))
        .mount(&server)
        .await;

    Mock::given(method("GET"))
        .and(path("/orgs/owner/packages/container/repo/versions"))
        .and(query_param("page", "2"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!([])))
        .mount(&server)
        .await;

    let client = reqwest::Client::new();
    let tags = fetch_ghcr_tags(
        &client,
        "ghcr.io/owner/repo",
        "gh_token",
        None,
        &server.uri(),
    )
    .await;
    assert_eq!(tags, vec!["v1.0.0", "v1.1.0", "v0.9.0"]);
}

#[tokio::test]
async fn test_fallback_to_user_endpoint_on_404() {
    let server = MockServer::start().await;

    // Org endpoint returns 404
    Mock::given(method("GET"))
        .and(path("/orgs/owner/packages/container/repo/versions"))
        .respond_with(ResponseTemplate::new(404))
        .mount(&server)
        .await;

    // User endpoint returns data on page 1
    Mock::given(method("GET"))
        .and(path("/users/owner/packages/container/repo/versions"))
        .and(query_param("page", "1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!([
            {"metadata": {"container": {"tags": ["v1.0.0"]}}}
        ])))
        .mount(&server)
        .await;

    // User endpoint returns empty on page 2
    Mock::given(method("GET"))
        .and(path("/users/owner/packages/container/repo/versions"))
        .and(query_param("page", "2"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!([])))
        .mount(&server)
        .await;

    let client = reqwest::Client::new();
    let tags = fetch_ghcr_tags(
        &client,
        "ghcr.io/owner/repo",
        "gh_token",
        None,
        &server.uri(),
    )
    .await;
    assert_eq!(tags, vec!["v1.0.0"]);
}

#[tokio::test]
async fn test_early_stop_on_current_tag() {
    let server = MockServer::start().await;

    Mock::given(method("GET"))
        .and(path("/orgs/owner/packages/container/repo/versions"))
        .and(query_param("page", "1"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!([
            {"metadata": {"container": {"tags": ["v2.0.0"]}}},
            {"metadata": {"container": {"tags": ["v1.5.0"]}}}
        ])))
        .mount(&server)
        .await;

    let client = reqwest::Client::new();
    // current_tag is on page 1 → should not fetch page 2
    let tags = fetch_ghcr_tags(
        &client,
        "ghcr.io/owner/repo",
        "gh_token",
        Some("v1.5.0"),
        &server.uri(),
    )
    .await;
    assert!(tags.contains(&"v2.0.0".to_string()));
    assert!(tags.contains(&"v1.5.0".to_string()));
}
