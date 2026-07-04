/**
 * Build a `uvpreview://` URL from an absolute file path, cross-platform.
 *
 * A Windows path used directly (`uvpreview://C:\runs\x.glb`) is an INVALID URL:
 * the `//` makes `C:` parse as an authority (host:port), which rejects. macOS
 * paths only worked because their leading `/` left the authority empty. So:
 * backslashes -> forward slashes, guarantee a leading `/` (empty authority),
 * and percent-encode so spaces/#/? survive URL parsing. The main-process
 * handler decodes and strips the extra slash before a drive letter.
 */
export function previewUrl(absPath: string): string {
  const p = absPath.replace(/\\/g, '/');
  const encoded = encodeURI(p).replace(/#/g, '%23').replace(/\?/g, '%3F');
  return `uvpreview://${p.startsWith('/') ? '' : '/'}${encoded}`;
}
