/* The staff console's one API client (shared by every page module). */
import { makeClient } from '/assets/api.js';

export const client = makeClient('staff');
export const { api } = client;
