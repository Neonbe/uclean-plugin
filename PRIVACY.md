# Privacy

UClean 0.1.0 runs locally. It has no analytics, telemetry, remote API, advertising, or automatic file upload. The optional panel binds to `127.0.0.1` and loads no remote scripts, fonts, or images. It uses a session token with a four-hour lifetime, checks the Host header and request Origin, and disables HTTP request logging.

Scans read file and directory metadata, not personal file contents. Git evidence checks read repository metadata. Process and open-file checks inspect local process information and return only an availability decision; process arguments are not written to reports. The scanner does not hash file contents or inspect document text.

Plans and operation receipts retain exact paths, metadata fingerprints, sizes, selected IDs and results in `~/Library/Application Support/UCleanPlugin`, with a private directory and files. A local key authenticates those records. Recovery records may outlive normal retention so that interrupted operations and trashed files remain identifiable. The `records --prune-days 30` command removes old ordinary records on request; it preserves recovery receipts and their associated plans. Configuration is retained. Uninstalling the plugin does not delete user files or reports.

If you ask an AI assistant to read scan results, paths and filenames in those results may become part of that provider's conversation. `--redact` conceals candidate paths in CLI output but does not remove exact paths from local records. Use the local panel to review individual filenames without returning them to chat. The publisher does not receive these records. Do not share the panel URL because its fragment contains a session token.

You can delete ordinary local records yourself after checking whether you still need any receipts or Trash restoration information. Removing the local signing key makes existing records unverifiable, so do not delete it while you need those records.

The plugin requires no account. GitHub or the AI client may handle installation, downloads, authentication and conversation data under their own policies. Support reports should omit private paths and session URLs. Contact: [GitHub Issues](https://github.com/Neonbe/uclean-plugin/issues).
