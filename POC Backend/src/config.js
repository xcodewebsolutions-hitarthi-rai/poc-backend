import "dotenv/config";

function requiredSecret() {
  const secret = process.env.JWT_SECRET;
  if (!secret || secret.length < 32) {
    throw new Error("JWT_SECRET must be set to at least 32 characters.");
  }
  return secret;
}

const port = Number(process.env.PORT ?? 8000);
const tokenExpiresSeconds = Number(process.env.JWT_EXPIRES_SECONDS ?? 3600);

if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error("PORT must be a valid TCP port.");
}
if (!Number.isInteger(tokenExpiresSeconds) || tokenExpiresSeconds < 1) {
  throw new Error("JWT_EXPIRES_SECONDS must be a positive integer.");
}

export const config = {
  port,
  demoEmail: process.env.DEMO_EMAIL ?? "demo@example.com",
  demoPassword: process.env.DEMO_PASSWORD ?? "ChangeMe123!",
  jwtSecret: requiredSecret(),
  jwtExpiresIn: tokenExpiresSeconds,
  jwtExpiresSeconds: tokenExpiresSeconds,
  corsOrigins: (process.env.FRONTEND_ORIGINS ?? "http://localhost:3000,http://localhost:5173")
    .split(",")
    .map((origin) => origin.trim())
    .filter(Boolean),
};
