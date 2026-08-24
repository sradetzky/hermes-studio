const encode = value => encodeURIComponent(value);

export const apiPaths = {
  projects: '/api/projects',
  profiles: '/api/profiles',
  comfyQueue: '/api/comfyui/queue',
  project: project => `/api/project/${encode(project)}`,
  movie: project => `/api/project/${encode(project)}/movie`,
  clips: project => `/api/project/${encode(project)}/clips`,
  clip: (project, clip) =>
    `/api/project/${encode(project)}/clips/${encode(clip)}`,
  clipOrder: project => `/api/project/${encode(project)}/clips/order`,
  chat: (project, after = 0) =>
    `/api/project/${encode(project)}/chat?after=${encode(after)}`,
  projectChat: project => `/api/project/${encode(project)}/chat`,
  clipChat: (project, clip, after = null) =>
    `/api/project/${encode(project)}/clips/${encode(clip)}/chat` +
    (after === null ? '' : `?after=${encode(after)}`),
  references: project => `/api/project/${encode(project)}/references`,
  jobs: project => `/api/project/${encode(project)}/jobs?limit=5`,
  events: (project, after = 0) =>
    `/api/project/${encode(project)}/events?after=${encode(after)}`,
  clipEvents: (project, clip, after = 0) =>
    `/api/project/${encode(project)}/clips/${encode(clip)}/events` +
    `?after=${encode(after)}`,
  generationSettings: (project, clip) =>
    `/api/project/${encode(project)}/clips/${encode(clip)}/generation-settings`,
  generate: (project, clip) =>
    `/api/project/${encode(project)}/clips/${encode(clip)}/generate`,
  generations: (project, clip) =>
    `/api/project/${encode(project)}/clips/${encode(clip)}/generations`,
  selectedTake: (project, clip) =>
    `/api/project/${encode(project)}/clips/${encode(clip)}/selected-take`,
  generation: (project, clip, generation) =>
    `/api/project/${encode(project)}/clips/${encode(clip)}/generations/` +
    `${encode(generation)}`,
  generationAction: (project, clip, generation, action) =>
    `/api/project/${encode(project)}/clips/${encode(clip)}/generations/` +
    `${encode(generation)}/${action}`,
};
