export const INVALID_LOCAL_NAVIGATION_DESTINATION = "Invalid navigation destination. Use a local path beginning with a single slash.";

// A fixed structural origin keeps this parser pure and identical in server and
// browser code. Only its root-relative path is returned; this host is never used
// as a navigation destination or contacted.
const VALIDATION_ORIGIN = "https://navigation.invalid";
const UNSAFE_CHARACTERS = /[\\\u0000-\u001f\u007f-\u009f]/;

export function getLocalNavigationDestination(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;

  try {
    const decoded = decodeURIComponent(value);
    if (
      !value.startsWith("/") || value.startsWith("//") ||
      decoded.startsWith("//") ||
      UNSAFE_CHARACTERS.test(value) || UNSAFE_CHARACTERS.test(decoded)
    ) throw new Error(INVALID_LOCAL_NAVIGATION_DESTINATION);

    const target = new URL(value, VALIDATION_ORIGIN);
    const localPath = `${target.pathname}${target.search}${target.hash}`;
    if (
      target.origin !== VALIDATION_ORIGIN ||
      localPath.startsWith("//") || decodeURIComponent(target.pathname).startsWith("//") ||
      new URL(localPath, VALIDATION_ORIGIN).origin !== VALIDATION_ORIGIN
    ) throw new Error(INVALID_LOCAL_NAVIGATION_DESTINATION);

    return localPath;
  } catch {
    throw new Error(INVALID_LOCAL_NAVIGATION_DESTINATION);
  }
}
