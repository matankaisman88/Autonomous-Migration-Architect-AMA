FROM node:20-slim AS builder
WORKDIR /app
ARG VITE_AMA_API_BASE=http://localhost:8000
ENV VITE_AMA_API_BASE=$VITE_AMA_API_BASE
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 3000
