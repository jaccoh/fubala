# Worker pods: design

Status: approved, updated for rulings (a)-(d). Story 1a ships this document and the worker image only;
Fubala's runtime behaviour does not change. Later stories (1b, 1c, ...) implement the rest.

## 1. Goal

Run the coder rounds of a story in a short-lived Kubernetes pod instead of inside the Fubala server
process. The server keeps the board, the pipeline state and the review stages. A worker pod gets a copy
of the workspace, runs the headless coder, and streams the result back. The pod holds no state worth
keeping and nothing it can use against the cluster.

## 2. Components

- **Fubala pod** (backend): creates, execs into and deletes worker pods through the Kubernetes API.
- **Worker pod**: image `$FUBALA_WORKER_IMAGE_REPO` (for example `registry.example.com/fubala/fubala-worker`) (section 5). Runs as uid/gid 10003 under
  tini. Contains git, node, uv, Python 3.12 and 3.15, libgomp and the claude CLI. No Fubala code.
- **Gitea**: the only source of truth for project code. Workers reach it for dependency fetches only
  where the story needs it; they never push.

## 3. Pod lifecycle

One worker pod per story round. The backend creates it with `ownerReferences` pointing at the Fubala
pod (so a crashed Fubala leaves no orphans) and a label `fubala/story=<id>`. The container command is
the image's tini plus a long sleep (the watchdog and CMD arrive in story 1c). The backend waits for
Ready, runs the round by exec, collects the result, and deletes the pod. A reaper deletes labelled pods
older than the round timeout.

## 4. Auth (ruling a)

- The `CLAUDE_CODE_OAUTH_TOKEN` setup-token reaches a worker pod only on **exec stdin**. It never goes
  in argv, in the pod spec, in an env var of the pod spec, in a Secret or in a ConfigMap.
- The setup-token has no refresh token; when it expires the owner issues a new one. Fubala stores it
  like its other provider keys (in the DB).
- **Verify pods get no model credential.** The review and verify stages run no model call from a
  worker pod; only the coder round receives the token, per exec.

## 5. Worker image and build

- `docker/worker.Dockerfile` uses the same `NODE_IMAGE`, `UV_IMAGE` and claude pin as the runtime
  `Dockerfile`; `backend/tests/test_worker_image_parity.py` fails when they drift.
- User `fubala` is 10003:10003. `/opt/uv-python`, `/opt/claude-code` and `/home/fubala` are owned by
  10003, so a read-only root filesystem with those paths as volumes, or a writable layer, both work.
- The last `RUN` of the Dockerfile runs `docker/worker-smoke.sh`: a red smoke means no image. The same
  script runs again in the restricted pod of the workflow.
- `.gitea/workflows/worker-image.yaml` builds `linux/amd64` and `linux/arm64` in two kaniko pods in
  the runner namespace (`gitea`, as `build.yaml`), each with `--custom-platform`. The in-build smoke
  is the per-arch smoke: containerd will not pull a single-arch manifest for a foreign arch, so a
  separate foreign-arch pod is not possible.
- **binfmt.** Each build pod has a privileged `tonistiigi/binfmt --install all` initContainer. It runs
  in the runner namespace, **never in the fubala namespace**. Build pods carry no node name and no
  `kubernetes.io/arch`: binfmt lets any node build either arch. They follow the worker placement
  rules: a toleration for `<FUBALA_DEV_NODE_LABEL>=true:NoSchedule` and a preferred node affinity (weight 100)
  for `<FUBALA_DEV_NODE_LABEL>=true`, so the qemu arm64 build does not always land on stor01. This applies
  to the kaniko, crane and smoke pods. binfmt registration is host-wide and persists on that node.
  The privileged binfmt image and the crane image are pinned by exact version plus digest, never
  by `latest`.
- When both arch builds are green, a crane pod assembles `<TAG>-candidate` (an index of both). A pod
  with the full restricted securityContext runs the smoke from the candidate. Only on green does
  `crane copy` publish `<TAG>` and `latest`. If either arch fails, neither tag moves.
- The restricted pod proves PSA conformance only on whichever arch its node runs. qemu does not
  reproduce Asahi's 16K pages, so a smoke pass is not proof for the Mac mini: **smoke on the node
  before labelling** it as a worker node.

## 6. Scheduling

Worker pods may run on amd64 and arm64 nodes (multi-arch image). The pod spec sets no arch selector;
a node is eligible once it has passed a node smoke. Resource requests and limits are per project and
come from settings (story 1b).

## 7. Workspace in (ruling b)

1. Fubala makes a **snapshot commit** of the story worktree through a temporary index
   (`GIT_INDEX_FILE`), so the real index and branch are never touched and untracked, non-ignored files
   are included.
2. It builds a `git bundle` of that commit and **streams it over exec stdin** into the pod, where the
   pod unbundles it into its workspace (`snapshot_bundle`).
3. There is no `fubala-run/<id>` ref pushed to Gitea, no initContainer that holds a Gitea token, and
   no credential for the workspace in the pod at all. The pod has nothing to authenticate to Gitea
   with.

## 8. Workspace out

The round result returns over exec stdout as a bundle or patch of the changes. Fubala applies it to
the story worktree and runs its normal diff, review and commit steps on the server side. The pod never
commits to the story branch.

## 9. Failure handling

A round that loses its exec stream (pod evicted, node gone, timeout) is a failed round, not a failed
story: the existing park/retry paths apply. The pod is deleted either way; the ownerReference and the
reaper cover a crashed Fubala.

## 10. Settings and code map

- Settings: worker mode on/off per project, worker image reference, resource limits, round timeout.
- Backend modules: pod create/exec/delete, `snapshot_bundle` (section 7), result apply (section 8).
- Default is off; with it off, behaviour is exactly today's in-process pipeline.

## 11. Pod Security (ruling c)

- Pod Security Admission `restricted` applies to the **whole fubala namespace**, because an
  ownerReference cannot point across namespaces: worker pods must live next to the Fubala pod that
  owns them. The Fubala pod itself therefore has to be restricted-conformant. Its required
  securityContext:
  - pod: `runAsNonRoot: true`, `runAsUser: 10003`, `runAsGroup: 10003`, `fsGroup: 10003`,
    `seccompProfile.type: RuntimeDefault`;
  - container: `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, no `privileged`, no
    host namespaces, no hostPath volumes;
  - allowed volume types only (emptyDir, PVC, configMap, secret, projected, downwardAPI).
- Rollout: label the namespace `pod-security.kubernetes.io/warn=restricted` and `audit=restricted`
  first; watch for warnings; switch to `enforce=restricted` only after **zero warnings**.

## 12. Network (ruling d)

- The Fubala pod reaches the Kubernetes API server through a **CiliumNetworkPolicy** egress rule with
  `toEntities: [kube-apiserver]`. This replaces any "6443 endpoints" rule.
- Worker pods have no ServiceAccount token (`automountServiceAccountToken: false`) and their egress is
  **DNS, Gitea and world:443 only**. They cannot reach the API server, other namespaces or Fubala.
- The existing Fubala NetworkPolicy (DNS, Gitea, internet on 443) stays.

## 13. RBAC

A Role in the fubala namespace for the Fubala ServiceAccount: create/get/list/delete/watch `pods`,
create `pods/exec`, get `pods/log`. Nothing cluster-wide.

## 14. Requirements R1-R8

The R1-R8 definitions come from story c5d0bf27, which was not reachable when this was written. The
one-line definitions below are inferred from how this design uses each tag: **to confirm by
coordinator**.

| Req | Requirement (inferred) | Sections |
|-----|------------------------|----------|
| R1 | Coder rounds run in an isolated, short-lived pod, not in the server process | 1, 2, 3 |
| R2 | The model credential never lands in argv, the pod spec or any stored object | 4 |
| R3 | The workspace goes in and out without giving the pod any Git credential | 7, 8 |
| R4 | The worker image has the same toolchain pins as the runtime image, on amd64 and arm64 | 5, 6 |
| R5 | The pod runs under Pod Security `restricted` as non-root uid 10003 | 5, 11 |
| R6 | Network egress is least-privilege; workers cannot reach the API server | 12, 13 |
| R7 | No orphaned pods: ownerReference, reaper and failure handling | 3, 9 |
| R8 | Behaviour is opt-in and reversible; the in-process pipeline stays the default | 10 |
