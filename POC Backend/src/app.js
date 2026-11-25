import cors from "cors";
import express from "express";
import swaggerUi from "swagger-ui-express";
import { authRouter } from "./auth.js";
import { config } from "./config.js";

export const app = express();

const openApiSpec = {
  openapi: "3.0.3",
  info: {
    title: "Frontend Login API",
    version: "1.0.0",
    description: "Stateless login API with configurable demo credentials.",
  },
  servers: [{ url: "/api/v1" }],
  paths: {
    "/health": {
      get: {
        summary: "Check API availability",
        tags: ["operations"],
        servers: [{ url: "/" }],
        responses: {
          "200": {
            description: "API is available",
            content: {
              "application/json": {
                schema: {
                  type: "object",
                  required: ["status"],
                  properties: { status: { type: "string", example: "ok" } },
                },
              },
            },
          },
        },
      },
    },
    "/auth/login": {
      post: {
        summary: "Log in with the configured demo credentials",
        tags: ["authentication"],
        requestBody: {
          required: true,
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/LoginRequest" },
            },
          },
        },
        responses: {
          "200": {
            description: "Login successful",
            content: {
              "application/json": {
                schema: { $ref: "#/components/schemas/LoginResponse" },
              },
            },
          },
          "400": { description: "Invalid request data" },
          "401": { description: "Invalid email or password" },
        },
      },
    },
    "/auth/me": {
      get: {
        summary: "Get the authenticated user",
        tags: ["authentication"],
        security: [{ bearerAuth: [] }],
        responses: {
          "200": {
            description: "Authenticated user",
            content: {
              "application/json": {
                schema: { $ref: "#/components/schemas/User" },
              },
            },
          },
          "401": { description: "Missing, invalid, or expired token" },
        },
      },
    },
  },
  components: {
    securitySchemes: {
      bearerAuth: { type: "http", scheme: "bearer", bearerFormat: "JWT" },
    },
    schemas: {
      LoginRequest: {
        type: "object",
        required: ["email", "password"],
        additionalProperties: false,
        properties: {
          email: { type: "string", format: "email", example: "demo@example.com" },
          password: { type: "string", minLength: 1, example: "ChangeMe123!" },
        },
      },
      LoginResponse: {
        type: "object",
        required: ["access_token", "token_type", "expires_in", "user"],
        properties: {
          access_token: { type: "string" },
          token_type: { type: "string", example: "bearer" },
          expires_in: { type: "integer", example: 3600 },
          user: { $ref: "#/components/schemas/User" },
        },
      },
      User: {
        type: "object",
        required: ["email"],
        properties: { email: { type: "string", format: "email" } },
      },
    },
  },
};

app.use(cors({
  origin(origin, callback) {
    return callback(null, !origin || config.corsOrigins.includes(origin));
  },
  methods: ["GET", "POST"],
  allowedHeaders: ["Authorization", "Content-Type"],
}));
app.use(express.json({ limit: "10kb" }));
app.get("/openapi.json", (_req, res) => res.json(openApiSpec));
app.use("/docs", swaggerUi.serve, swaggerUi.setup(openApiSpec));
app.get("/health", (_req, res) => res.json({ status: "ok" }));
app.use("/api/v1/auth", authRouter);

app.use((err, _req, res, _next) => {
  if (err instanceof SyntaxError && "body" in err) {
    return res.status(400).json({ detail: "Invalid JSON request body" });
  }
  console.error(err);
  return res.status(500).json({ detail: "Internal server error" });
});
