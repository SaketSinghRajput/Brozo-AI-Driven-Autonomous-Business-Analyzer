# syntax=docker/dockerfile:1
# Frontend image (build context: frontend/): Vite build served as static files by an
# unprivileged nginx (infra/compose `web` service). TASK-INFRA-001.

# node:24.15.0 matches the local dev Node (v24.15.0) and package.json "engines": "^24.0.0".
FROM node:24.15.0-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# nginxinc/nginx-unprivileged runs as a non-root user (uid 101) and listens on 8080 by
# default - no privileged port, no USER directive needed.
FROM nginxinc/nginx-unprivileged:1.29-alpine AS runtime
# Pick up OS security fixes newer than the base image (SEC-014), then drop back to uid 101.
USER root
RUN apk upgrade --no-cache
USER 101
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080
