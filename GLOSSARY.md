# Glossary

## image-override

Label (`docker-update-monitor.image-override`) naming the upstream image a locally built image derives from. The update check targets the upstream reference — full image name plus the tag currently tracked — instead of the local image reference, which has no registry presence of its own. Semver (tag-regex) detection only; container name and stack remain the container's own.
