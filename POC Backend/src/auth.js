import { timingSafeEqual } from "node:crypto";
import { Router } from "express";
import jwt from "jsonwebtoken";
import { z } from "zod";
import { config } from "./config.js";

export const authRouter = Router();

const loginSchema = z.object({
  email: z.string().email(),
  password: z.string().min(1),
}).strict();

function constantTimeEqual(left, right) {
  const leftBuffer = Buffer.from(left);
  const rightBuffer = Buffer.from(right);
  return leftBuffer.length === rightBuffer.length
    && timingSafeEqual(leftBuffer, rightBuffer);
}

export function authenticateToken(req, res, next) {
  const authorization = req.get("authorization") ?? "";
  const [scheme, token] = authorization.split(" ");

  if (scheme !== "Bearer" || !token) {
    return res.status(401).json({ detail: "Not authenticated" });
  }

  try {
    const payload = jwt.verify(token, config.jwtSecret);
    if (typeof payload !== "object" || payload.sub?.toLowerCase() !== config.demoEmail.toLowerCase()) {
      return res.status(401).json({ detail: "Invalid or expired token" });
    }
    req.user = { email: payload.sub };
    return next();
  } catch {
    return res.status(401).json({ detail: "Invalid or expired token" });
  }
}

authRouter.post("/login", (req, res) => {
  const parsed = loginSchema.safeParse(req.body);
  if (!parsed.success) {
    return res.status(400).json({
      detail: "Invalid request",
      errors: parsed.error.issues.map(({ path, message }) => ({ field: path.join("."), message })),
    });
  }

  const emailMatches = constantTimeEqual(
    parsed.data.email.toLowerCase(),
    config.demoEmail.toLowerCase(),
  );
  const passwordMatches = constantTimeEqual(parsed.data.password, config.demoPassword);
  if (!emailMatches || !passwordMatches) {
    return res.status(401).json({ detail: "Invalid email or password" });
  }

  const accessToken = jwt.sign(
    { sub: parsed.data.email },
    config.jwtSecret,
    { expiresIn: config.jwtExpiresIn },
  );

  return res.json({
    access_token: accessToken,
    token_type: "bearer",
    expires_in: config.jwtExpiresSeconds,
    user: { email: parsed.data.email },
  });
});

authRouter.get("/me", authenticateToken, (req, res) => {
  res.json(req.user);
});
