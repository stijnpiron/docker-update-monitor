use crate::config::Config;
use crate::models::{RegexMismatch, ScanWarning, UpdateInfo};
use anyhow::Result;
use chrono::Local;
use lettre::message::{header::ContentType, MultiPart, SinglePart};
use lettre::transport::smtp::authentication::Credentials;
use lettre::{Address, Message, SmtpTransport, Transport};

const TYPE_COLORS: &[(&str, &str)] = &[
    ("major", "#dc2626"),
    ("minor", "#d97706"),
    ("patch", "#2563eb"),
];
const TD: &str = "padding:6px 12px;border-bottom:1px solid #e5e7eb;";
const MONO: &str =
    "padding:6px 12px;border-bottom:1px solid #e5e7eb;font-family:monospace;font-size:13px;";

fn escape_html(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

fn type_color(update_type: &str) -> &'static str {
    TYPE_COLORS
        .iter()
        .find(|(t, _)| *t == update_type)
        .map(|(_, c)| *c)
        .unwrap_or("#6b7280")
}

fn split_by_status(
    updates: &[UpdateInfo],
) -> (Vec<&UpdateInfo>, Vec<&UpdateInfo>, Vec<&UpdateInfo>) {
    let mut new = vec![];
    let mut known = vec![];
    let mut resolved = vec![];
    for u in updates {
        match u.status.as_str() {
            "new" => new.push(u),
            "known" => known.push(u),
            "resolved" => resolved.push(u),
            _ => {}
        }
    }
    (new, known, resolved)
}

fn sort_updates(mut updates: Vec<&UpdateInfo>) -> Vec<&UpdateInfo> {
    updates.sort_by(|a, b| a.stack.cmp(&b.stack).then_with(|| a.image.cmp(&b.image)));
    updates
}

fn build_rows(updates: &[&UpdateInfo]) -> String {
    let mut rows = String::new();
    for u in updates {
        let color = type_color(&u.update_type);
        let service = if u.service_name.is_empty() {
            &u.container_name
        } else {
            &u.service_name
        };
        let current = if u.current_version.is_empty() {
            "\u{2014}".to_string()
        } else {
            escape_html(&u.current_version)
        };
        rows.push_str(&format!(
            "<tr>\
            <td style=\"{TD}font-weight:bold;\">{stack}</td>\
            <td style=\"{TD}\">{service}</td>\
            <td style=\"{TD}\">{container}</td>\
            <td style=\"{TD}\">{image}</td>\
            <td style=\"{MONO}\">{current}</td>\
            <td style=\"{MONO}color:{color};font-weight:bold;\">{new_ver}</td>\
            <td style=\"{TD}color:{color};font-weight:bold;\">{update_type}</td>\
            </tr>",
            stack = escape_html(&u.stack),
            service = escape_html(service),
            container = escape_html(&u.container_name),
            image = escape_html(&u.image),
            current = current,
            color = color,
            new_ver = escape_html(&u.new_version),
            update_type = escape_html(&u.update_type),
        ));
    }
    rows
}

fn build_section(title: &str, emoji: &str, updates: &[&UpdateInfo], header_color: &str) -> String {
    if updates.is_empty() {
        return String::new();
    }
    format!(
        "<h3 style=\"color:{header_color};margin:20px 0 8px 0;\">{emoji} {title} ({count})</h3>\
        <table style=\"border-collapse:collapse;width:100%;\">\
        <thead><tr style=\"background:#f3f4f6;\">\
        <th style=\"padding:6px 12px;text-align:left;\">Stack</th>\
        <th style=\"padding:6px 12px;text-align:left;\">Service</th>\
        <th style=\"padding:6px 12px;text-align:left;\">Container</th>\
        <th style=\"padding:6px 12px;text-align:left;\">Image</th>\
        <th style=\"padding:6px 12px;text-align:left;\">Current</th>\
        <th style=\"padding:6px 12px;text-align:left;\">Latest</th>\
        <th style=\"padding:6px 12px;text-align:left;\">Type</th>\
        </tr></thead>\
        <tbody>{rows}</tbody>\
        </table>",
        header_color = header_color,
        emoji = emoji,
        title = title,
        count = updates.len(),
        rows = build_rows(updates),
    )
}

pub fn build_mismatch_section(mismatches: &[RegexMismatch]) -> String {
    if mismatches.is_empty() {
        return String::new();
    }
    let mut rows = String::new();
    for m in mismatches {
        let service = if m.service_name.is_empty() {
            &m.container_name
        } else {
            &m.service_name
        };
        rows.push_str(&format!(
            "<tr>\
            <td style=\"{TD}font-weight:bold;\">{stack}</td>\
            <td style=\"{TD}\">{service}</td>\
            <td style=\"{TD}\">{container}</td>\
            <td style=\"{TD}\">{image}</td>\
            <td style=\"{MONO}\">{tag}</td>\
            <td style=\"{MONO}\">{pattern}</td>\
            </tr>",
            stack = escape_html(&m.stack),
            service = escape_html(service),
            container = escape_html(&m.container_name),
            image = escape_html(&m.image),
            tag = escape_html(&m.current_tag),
            pattern = escape_html(&m.pattern),
        ));
    }
    format!(
        "<h3 style=\"color:#6b7280;margin:20px 0 8px 0;\">\u{26a0}\u{fe0f} Regex mismatches ({count})</h3>\
        <p style=\"color:#6b7280;font-size:13px;margin:0 0 8px 0;\">\
        These containers have a tag-regex that does not match their current tag. Check your configuration.</p>\
        <table style=\"border-collapse:collapse;width:100%;\">\
        <thead><tr style=\"background:#f3f4f6;\">\
        <th style=\"padding:6px 12px;text-align:left;\">Stack</th>\
        <th style=\"padding:6px 12px;text-align:left;\">Service</th>\
        <th style=\"padding:6px 12px;text-align:left;\">Container</th>\
        <th style=\"padding:6px 12px;text-align:left;\">Image</th>\
        <th style=\"padding:6px 12px;text-align:left;\">Current Tag</th>\
        <th style=\"padding:6px 12px;text-align:left;\">Pattern</th>\
        </tr></thead>\
        <tbody>{rows}</tbody>\
        </table>",
        count = mismatches.len(),
        rows = rows,
    )
}

pub fn build_warnings_section(warnings: &[ScanWarning]) -> String {
    if warnings.is_empty() {
        return String::new();
    }
    let mut rows = String::new();
    for w in warnings {
        let color = if w.level == "error" {
            "#dc2626"
        } else {
            "#d97706"
        };
        let image = if w.image.is_empty() {
            "\u{2014}".to_string()
        } else {
            escape_html(&w.image)
        };
        rows.push_str(&format!(
            "<tr>\
            <td style=\"{TD}color:{color};font-weight:bold;\">{level}</td>\
            <td style=\"{TD}\">{container}</td>\
            <td style=\"{TD}\">{image}</td>\
            <td style=\"{TD}\">{message}</td>\
            </tr>",
            color = color,
            level = escape_html(&w.level.to_uppercase()),
            container = escape_html(&w.container_name),
            image = image,
            message = escape_html(&w.message),
        ));
    }
    format!(
        "<h3 style=\"color:#d97706;margin:20px 0 8px 0;\">\u{1f6a8} Warnings &amp; errors ({count})</h3>\
        <table style=\"border-collapse:collapse;width:100%;\">\
        <thead><tr style=\"background:#f3f4f6;\">\
        <th style=\"padding:6px 12px;text-align:left;\">Level</th>\
        <th style=\"padding:6px 12px;text-align:left;\">Container</th>\
        <th style=\"padding:6px 12px;text-align:left;\">Image</th>\
        <th style=\"padding:6px 12px;text-align:left;\">Message</th>\
        </tr></thead>\
        <tbody>{rows}</tbody>\
        </table>",
        count = warnings.len(),
        rows = rows,
    )
}

pub fn build_html(
    updates: &[UpdateInfo],
    mismatches: &[RegexMismatch],
    warnings: &[ScanWarning],
) -> String {
    let (new, known, resolved) = split_by_status(updates);
    let new = sort_updates(new);
    let known = sort_updates(known);
    let resolved = sort_updates(resolved);

    let mut sections = String::new();
    sections.push_str(&build_section("New updates", "\u{1f195}", &new, "#dc2626"));
    sections.push_str(&build_section(
        "Known updates",
        "\u{1f504}",
        &known,
        "#d97706",
    ));
    sections.push_str(&build_section("Resolved", "\u{2705}", &resolved, "#16a34a"));
    sections.push_str(&build_mismatch_section(mismatches));
    sections.push_str(&build_warnings_section(warnings));

    let today = Local::now().format("%-d %B %Y").to_string();
    format!(
        "<html><body style=\"font-family:sans-serif;color:#111;\">\
        <h2 style=\"color:#1d4ed8;\">Docker image updates \u{2013} {today}</h2>\
        {sections}\
        <p style=\"color:#6b7280;font-size:12px;margin-top:16px;\">Sent by Docker Update Monitor</p>\
        </body></html>",
        today = today,
        sections = sections,
    )
}

pub fn build_plain(
    updates: &[UpdateInfo],
    mismatches: &[RegexMismatch],
    warnings: &[ScanWarning],
) -> String {
    let (new, known, resolved) = split_by_status(updates);
    let new = sort_updates(new);
    let known = sort_updates(known);
    let resolved = sort_updates(resolved);

    let mut lines: Vec<String> = vec![
        "Docker Update Monitor".to_string(),
        "=".repeat(40),
        String::new(),
    ];

    for (title, group) in [
        ("New updates", &new),
        ("Known updates", &known),
        ("Resolved", &resolved),
    ] {
        if group.is_empty() {
            continue;
        }
        lines.push(format!("{} ({})", title, group.len()));
        lines.push("-".repeat(30));
        for u in group {
            let service = if u.service_name.is_empty() {
                &u.container_name
            } else {
                &u.service_name
            };
            lines.push(format!(
                "  [{}] {} ({}) {} {} -> {} ({})",
                u.stack,
                service,
                u.container_name,
                u.image,
                u.current_version,
                u.new_version,
                u.update_type,
            ));
        }
        lines.push(String::new());
    }

    if !mismatches.is_empty() {
        lines.push(format!("Regex mismatches ({})", mismatches.len()));
        lines.push("-".repeat(30));
        for m in mismatches {
            let service = if m.service_name.is_empty() {
                &m.container_name
            } else {
                &m.service_name
            };
            lines.push(format!(
                "  [{}] {} ({}) {}:{}  pattern='{}'",
                m.stack, service, m.container_name, m.image, m.current_tag, m.pattern,
            ));
        }
        lines.push(String::new());
    }

    if !warnings.is_empty() {
        lines.push(format!("Warnings ({})", warnings.len()));
        lines.push("-".repeat(30));
        for w in warnings {
            let image_part = if w.image.is_empty() {
                String::new()
            } else {
                format!(" {}", w.image)
            };
            lines.push(format!(
                "  [{}] {}{}: {}",
                w.level.to_uppercase(),
                w.container_name,
                image_part,
                w.message,
            ));
        }
        lines.push(String::new());
    }

    lines.join("\n")
}

pub fn build_message(
    updates: &[UpdateInfo],
    mismatches: &[RegexMismatch],
    warnings: &[ScanWarning],
    config: &Config,
) -> Result<Message> {
    let total = updates.len();
    let suffix = if total == 1 { "update" } else { "updates" };
    let subject = format!(
        "\u{1f433} Docker Update Monitor \u{2013} {} image {}",
        total, suffix
    );

    let from: Address = config
        .smtp_from
        .parse()
        .map_err(|e| anyhow::anyhow!("invalid SMTP_FROM: {e}"))?;

    let mut builder = Message::builder()
        .from(lettre::message::Mailbox::new(None, from))
        .subject(subject);

    for to_addr in config
        .smtp_to
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        let addr: Address = to_addr
            .parse()
            .map_err(|e| anyhow::anyhow!("invalid SMTP_TO address '{to_addr}': {e}"))?;
        builder = builder.to(lettre::message::Mailbox::new(None, addr));
    }

    let plain_body = build_plain(updates, mismatches, warnings);
    let html_body = build_html(updates, mismatches, warnings);

    let message = builder.multipart(
        MultiPart::alternative()
            .singlepart(
                SinglePart::builder()
                    .header(ContentType::TEXT_PLAIN)
                    .body(plain_body),
            )
            .singlepart(
                SinglePart::builder()
                    .header(ContentType::TEXT_HTML)
                    .body(html_body),
            ),
    )?;

    Ok(message)
}

pub fn notify(
    updates: &[UpdateInfo],
    mismatches: &[RegexMismatch],
    warnings: &[ScanWarning],
    config: &Config,
) -> Result<()> {
    if updates.is_empty() && mismatches.is_empty() && warnings.is_empty() {
        return Ok(());
    }

    if config.smtp_host.is_empty() || config.smtp_from.is_empty() || config.smtp_to.is_empty() {
        tracing::warn!(
            "SMTP not fully configured (SMTP_HOST, SMTP_FROM, SMTP_TO required) — skipping email notification."
        );
        return Ok(());
    }

    let message = build_message(updates, mismatches, warnings, config)?;

    let creds = if !config.smtp_username.is_empty() && !config.smtp_password.is_empty() {
        Some(Credentials::new(
            config.smtp_username.clone(),
            config.smtp_password.clone(),
        ))
    } else {
        None
    };

    let transport = build_transport(config, creds)?;
    transport
        .send(&message)
        .map_err(|e| anyhow::anyhow!("Failed to send email notification: {e}"))?;

    tracing::info!(
        "Email sent to {} with {} update(s)",
        config.smtp_to,
        updates.len()
    );
    Ok(())
}

fn build_transport(config: &Config, creds: Option<Credentials>) -> Result<SmtpTransport> {
    let transport = if config.smtp_port == 465 {
        let mut b = SmtpTransport::relay(&config.smtp_host)
            .map_err(|e| anyhow::anyhow!("{e}"))?
            .port(config.smtp_port);
        if let Some(c) = creds {
            b = b.credentials(c);
        }
        b.build()
    } else if config.smtp_tls {
        let mut b = SmtpTransport::starttls_relay(&config.smtp_host)
            .map_err(|e| anyhow::anyhow!("{e}"))?
            .port(config.smtp_port);
        if let Some(c) = creds {
            b = b.credentials(c);
        }
        b.build()
    } else {
        let mut b = SmtpTransport::builder_dangerous(&config.smtp_host).port(config.smtp_port);
        if let Some(c) = creds {
            b = b.credentials(c);
        }
        b.build()
    };
    Ok(transport)
}
