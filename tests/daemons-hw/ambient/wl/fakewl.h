// What the plinius-ambient client test does as the compositor (see fakewl.c)
#pragma once

extern int fakewl_queued, fakewl_read_error, fakewl_dispatch_error;
void fakewl_output_events(int transform, int width, int height, int scale);
void fakewl_readable(void);
void fakewl_hangup(void);
int fakewl_frame_done(void);
int fakewl_pending_frames(void);
int fakewl_configure(int width, int height);
int fakewl_closed(void);
void fakewl_report(void);
