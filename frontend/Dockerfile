FROM --platform=$BUILDPLATFORM node:22-alpine AS build
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
COPY contracts/c2_contract_v3.json /src/contracts/c2_contract_v3.json
ARG OPS_READ_ONLY=0
RUN VITE_API_BASE_URL=/api VITE_OPS_READ_ONLY=$OPS_READ_ONLY npm run build

FROM nginx:stable-alpine
COPY --from=build /src/frontend/dist /usr/share/nginx/html
COPY frontend/docker/default.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
