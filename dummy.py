class ShellFileOperations:
    def _search_with_grep(self, pattern, path, file_glob, limit, offset, output_mode, context):
        cmd_parts = ["grep", "-rnH"]
        cmd_parts.append("--exclude-dir='.*'")
        cmd_parts.append(self._escape_shell_arg(path))
        cmd_parts.extend(["|", "head", "-n", str(fetch_limit)])
        cmd = "set -o pipefail; " + " ".join(cmd_parts)
