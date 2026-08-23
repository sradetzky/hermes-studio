const encode = value => encodeURIComponent(value);

export const apiPaths = {
  projects: '/api/projects',
  profiles: '/api/profiles',
  project: project => `/api/project/${encode(project)}`,
  clips: project => `/api/project/${encode(project)}/clips`,
  clip: (project, clip) =>
    `/api/project/${encode(project)}/clips/${encode(clip)}`,
  clipOrder: project => `/api/project/${encode(project)}/clips/order`,
  chat: (project, after = 0) =>
    `/api/project/${encode(project)}/chat?after=${encode(after)}`,
  clipChat: (project, clip) =>
    `/api/project/${encode(project)}/clips/${encode(clip)}/chat`,
  references: project => `/api/project/${encode(project)}/references`,
  jobs: project => `/api/project/${encode(project)}/jobs?limit=5`,
  events: (project, after = 0) =>
    `/api/project/${encode(project)}/events?after=${encode(after)}`,
  generationSettings: (project, clip) =>
    `/api/project/${encode(project)}/clips/${encode(clip)}/generation-settings`,
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
