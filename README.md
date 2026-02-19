# KubeRCA Helm Chart Repository

Helm chart repository for [KubeRCA](https://github.com/Kuberca-io/KubeRCA) — Kubernetes Root Cause Analysis System.

## Usage

```bash
helm repo add kuberca https://kuberca-io.github.io/KubeRCA
helm repo update
helm search repo kuberca
helm install kuberca kuberca/kuberca
```

## Available Charts

| Chart | Version | App Version |
|-------|---------|-------------|
| [kuberca](https://github.com/Kuberca-io/KubeRCA/tree/main/helm/kuberca) | 0.1.0 | 0.1.0 |

## OCI Alternative

Charts are also available as OCI artifacts:

```bash
helm install kuberca oci://ghcr.io/kuberca-io/charts/kuberca --version 0.1.0
```

## Links

- [Website](https://kuberca.io)
- [Source code](https://github.com/Kuberca-io/KubeRCA)
- [Documentation](https://kuberca.io)
- [Releases](https://github.com/Kuberca-io/KubeRCA/releases)
