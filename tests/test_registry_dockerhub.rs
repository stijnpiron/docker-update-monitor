use docker_update_monitor::registry::dockerhub::{fetch_dockerhub_tags, get_dockerhub_token};
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

#[tokio::test]
async fn test_get_token_success() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v2/users/login"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(serde_json::json!({"token": "hub-jwt-abc123"})),
        )
        .mount(&server)
        .await;

    let client = reqwest::Client::new();
    let login_url = format!("{}/v2/users/login", server.uri());
    let token = get_dockerhub_token(&client, "user", "pass", &login_url).await;
    assert_eq!(token, Some("hub-jwt-abc123".to_string()));
}

#[tokio::test]
async fn test_get_token_no_credentials_returns_none() {
    let client = reqwest::Client::new();
    // Passing a URL that would never be called
    let token = get_dockerhub_token(&client, "", "", "http://unused").await;
    assert!(token.is_none());
}

#[tokio::test]
async fn test_fetch_tags_single_page() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/v2/namespaces/library/repositories/nginx/tags"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "results": [{"name": "1.0.0"}, {"name": "1.1.0"}],
            "next": null
        })))
        .mount(&server)
        .await;

    let client = reqwest::Client::new();
    let tags = fetch_dockerhub_tags(&client, "nginx", None, None, &server.uri()).await;
    assert_eq!(tags, vec!["1.0.0", "1.1.0"]);
}

#[tokio::test]
async fn test_fetch_tags_early_stop_on_current_tag() {
    let server = MockServer::start().await;

    // Page 1 contains current_tag — pagination should stop here
    Mock::given(method("GET"))
        .and(path("/v2/namespaces/library/repositories/nginx/tags"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "results": [{"name": "2.0.0"}, {"name": "1.0.0"}],
            "next": "http://should-not-be-called/page2"
        })))
        .mount(&server)
        .await;

    let client = reqwest::Client::new();
    let tags = fetch_dockerhub_tags(&client, "nginx", None, Some("1.0.0"), &server.uri()).await;
    assert!(tags.contains(&"1.0.0".to_string()));
    assert!(tags.contains(&"2.0.0".to_string()));
    // Verify pagination stopped after first page (second page URL was never called)
    assert_eq!(tags.len(), 2);
}

#[tokio::test]
async fn test_fetch_tags_strips_docker_io_prefix() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/v2/namespaces/library/repositories/nginx/tags"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "results": [{"name": "latest"}],
            "next": null
        })))
        .mount(&server)
        .await;

    let client = reqwest::Client::new();
    let tags = fetch_dockerhub_tags(
        &client,
        "docker.io/library/nginx",
        None,
        None,
        &server.uri(),
    )
    .await;
    assert_eq!(tags, vec!["latest"]);
}
