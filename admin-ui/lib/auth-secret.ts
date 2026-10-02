export function requireAuthSecret(): string {
  const secret = process.env.AUTH_SECRET;
  if (!secret || secret.trim().length < 32) {
    throw new Error("AUTH_SECRET must be configured with a random value of at least 32 characters");
  }
  return secret;
}
