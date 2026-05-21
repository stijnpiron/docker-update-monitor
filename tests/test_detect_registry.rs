use docker_update_monitor::registry::{detect_registry, RegistryKind};
use rstest::rstest;

#[rstest]
#[case("nginx", RegistryKind::DockerHub)]
#[case("linuxserver/sonarr", RegistryKind::DockerHub)]
#[case("docker.io/library/nginx", RegistryKind::DockerHub)]
#[case("ghcr.io/owner/repo", RegistryKind::Ghcr)]
#[case("lscr.io/linuxserver/bazarr", RegistryKind::Ghcr)]
#[case("quay.io/prometheus/node-exporter", RegistryKind::Unknown)]
fn test_detect_registry(#[case] image: &str, #[case] expected: RegistryKind) {
    assert_eq!(detect_registry(image), expected);
}

#[test]
fn test_detect_registry_localhost_with_port_is_unknown() {
    assert_eq!(
        detect_registry("localhost:5000/image"),
        RegistryKind::Unknown
    );
}

#[test]
fn test_detect_registry_url_form_ghcr() {
    assert_eq!(
        detect_registry("https://ghcr.io/owner/repo"),
        RegistryKind::Ghcr
    );
}

#[test]
fn test_detect_registry_url_form_dockerhub() {
    assert_eq!(
        detect_registry("https://docker.io/library/nginx"),
        RegistryKind::DockerHub
    );
}
