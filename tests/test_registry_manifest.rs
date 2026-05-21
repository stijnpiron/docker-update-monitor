use docker_update_monitor::registry::manifest::{is_platform_supported, Platform};

fn platform(os: &str, arch: &str) -> Platform {
    Platform {
        os: os.into(),
        architecture: arch.into(),
        variant: None,
    }
}

#[test]
fn test_is_platform_supported_none_returns_true() {
    assert!(is_platform_supported(None, "linux", "amd64"));
    assert!(is_platform_supported(None, "linux", "arm64"));
}

#[test]
fn test_is_platform_supported_match() {
    let platforms = vec![platform("linux", "amd64"), platform("linux", "arm64")];
    assert!(is_platform_supported(Some(&platforms), "linux", "amd64"));
    assert!(is_platform_supported(Some(&platforms), "linux", "arm64"));
    assert!(!is_platform_supported(Some(&platforms), "linux", "arm"));
    assert!(!is_platform_supported(Some(&platforms), "windows", "amd64"));
}

#[test]
fn test_is_platform_supported_empty_list_returns_false() {
    assert!(!is_platform_supported(Some(&[]), "linux", "amd64"));
}
