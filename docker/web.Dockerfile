FROM node:20-slim AS builder
WORKDIR /app
ARG VITE_AMA_API_BASE=http://localhost:8000
ARG VITE_DEFAULT_REPORT_PATH=/app/sample_data/kfar_supply/kfar_report.json
ENV VITE_AMA_API_BASE=$VITE_AMA_API_BASE
ENV VITE_DEFAULT_REPORT_PATH=$VITE_DEFAULT_REPORT_PATH
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 3000
