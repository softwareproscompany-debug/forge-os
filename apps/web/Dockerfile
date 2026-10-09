# ---- Build stage ----
FROM node:20.18.1-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY index.html vite.config.ts tsconfig.json tsconfig.node.json .env.example ./
COPY src ./src
# VITE_API_URL can be overridden at build time: --build-arg VITE_API_URL=...
ARG VITE_API_URL=http://localhost:8000
ENV VITE_API_URL=${VITE_API_URL}
RUN npm run build

# ---- Serve stage ----
FROM nginx:1.27.3-alpine AS serve
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
