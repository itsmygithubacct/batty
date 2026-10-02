/* SPDX-License-Identifier: MIT */
#include "surface.h"
#include <GLES3/gl3.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>
#include <unistd.h>

struct BtSurface {
    SDL_Window *window;
    SDL_GLContext context;
    BtSurfaceEvent event;
    void *userdata;
    struct BtSurface *next;
    pid_t owner;
    char error[256];
};
static BtSurface *surfaces;
static int fail(BtSurface *s, const char *message) {
    snprintf(s->error,sizeof(s->error),"%s",message);
    return -1;
}
SDL_Window *bt_surface_window(BtSurface *s) { return s->window; }
SDL_GLContext bt_surface_context(BtSurface *s) { return s->context; }
const char *bt_surface_error(BtSurface *s) { return s->error; }
BtSurface *bt_surface_new(const char *title, int width, int height, char *error, size_t capacity) {
    BtSurface *s=calloc(1,sizeof(*s));
    if(!s) { snprintf(error,capacity,"Could not allocate window surface"); return NULL; }
    s->owner=getpid();
    if(SDL_InitSubSystem(SDL_INIT_VIDEO)) {
        snprintf(error,capacity,"%s",SDL_GetError()); free(s); return NULL;
    }
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK,SDL_GL_CONTEXT_PROFILE_ES);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION,3);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION,0);
    SDL_GL_SetAttribute(SDL_GL_DOUBLEBUFFER,1);
    s->window=SDL_CreateWindow(title,SDL_WINDOWPOS_CENTERED,SDL_WINDOWPOS_CENTERED,
        width,height,SDL_WINDOW_OPENGL|SDL_WINDOW_RESIZABLE|SDL_WINDOW_ALLOW_HIGHDPI);
    if(!s->window) goto failed;
    s->context=SDL_GL_CreateContext(s->window);
    if(!s->context || SDL_GL_MakeCurrent(s->window,s->context)) goto failed;
    SDL_GL_SetSwapInterval(0);
    SDL_SetWindowMinimumSize(s->window,120,80);
    SDL_StartTextInput();
    s->next=surfaces; surfaces=s;
    return s;
failed:
    snprintf(error,capacity,"%s",SDL_GetError());
    if(s->context) SDL_GL_DeleteContext(s->context);
    if(s->window) SDL_DestroyWindow(s->window);
    SDL_QuitSubSystem(SDL_INIT_VIDEO); free(s); return NULL;
}
void bt_surface_handler(BtSurface *s, BtSurfaceEvent event, void *userdata) {
    s->event=event; s->userdata=userdata;
}
static uint32_t event_window(const SDL_Event *e) {
    switch(e->type) {
        case SDL_WINDOWEVENT: return e->window.windowID;
        case SDL_KEYDOWN: case SDL_KEYUP: return e->key.windowID;
        case SDL_TEXTINPUT: return e->text.windowID;
        case SDL_TEXTEDITING: return e->edit.windowID;
#if SDL_VERSION_ATLEAST(2,0,22)
        case SDL_TEXTEDITING_EXT: return e->editExt.windowID;
#endif
        case SDL_MOUSEMOTION: return e->motion.windowID;
        case SDL_MOUSEBUTTONDOWN: case SDL_MOUSEBUTTONUP: return e->button.windowID;
        case SDL_MOUSEWHEEL: return e->wheel.windowID;
        default: return 0;
    }
}
void bt_surface_poll(void) {
    SDL_Event e;
    while(SDL_PollEvent(&e)) {
        uint32_t id=event_window(&e);
        for(BtSurface *s=surfaces;s;s=s->next)
            if(s->owner==getpid() && s->event &&
               (e.type==SDL_QUIT || (id && SDL_GetWindowID(s->window)==id)))
                s->event(s->userdata,&e);
#if SDL_VERSION_ATLEAST(2,0,22)
        if(e.type==SDL_TEXTEDITING_EXT) SDL_free(e.editExt.text);
#endif
    }
}
int bt_surface_clear(BtSurface *s, unsigned char red, unsigned char green, unsigned char blue) {
    if(SDL_GL_MakeCurrent(s->window,s->context)) return fail(s,SDL_GetError());
    int width,height; SDL_GL_GetDrawableSize(s->window,&width,&height);
    glViewport(0,0,width,height); glDisable(GL_SCISSOR_TEST);
    glClearColor(red/255.f,green/255.f,blue/255.f,1);
    glClear(GL_COLOR_BUFFER_BIT);
    return glGetError()==GL_NO_ERROR?0:fail(s,"Could not clear window surface");
}
int bt_surface_present(BtSurface *s) {
    if(SDL_GL_MakeCurrent(s->window,s->context)) return fail(s,SDL_GetError());
    SDL_GL_SwapWindow(s->window); return 0;
}
int bt_surface_capture(BtSurface *s, const char *path) {
    if(SDL_GL_MakeCurrent(s->window,s->context)) return fail(s,SDL_GetError());
    int width,height; SDL_GL_GetDrawableSize(s->window,&width,&height);
    uint64_t size=(uint64_t)width*(uint64_t)height*4;
    if(width<1 || height<1 || size>512u*1024u*1024u)
        return fail(s,"Window capture exceeds the 512 MiB limit");
    uint8_t *pixels=malloc((size_t)size);
    if(!pixels) return fail(s,"Could not allocate window capture");
    glReadPixels(0,0,width,height,GL_RGBA,GL_UNSIGNED_BYTE,pixels);
    if(glGetError()!=GL_NO_ERROR) { free(pixels); return fail(s,"Could not read window capture"); }
    FILE *f=fopen(path,"wb");
    if(!f) { free(pixels); return fail(s,"Could not open window capture"); }
    bool ok=fprintf(f,"P6\n%d %d\n255\n",width,height)>0;
    for(int y=height-1;y>=0 && ok;--y)
        for(int x=0;x<width;++x)
            if(fwrite(pixels+((size_t)y*width+x)*4,1,3,f)!=3) { ok=false; break; }
    free(pixels);
    if(fclose(f)) ok=false;
    return ok?0:fail(s,"Could not write window capture");
}
void bt_surface_free(BtSurface *s) {
    if(!s || s->owner!=getpid()) return;
    BtSurface **link=&surfaces;
    while(*link && *link!=s) link=&(*link)->next;
    if(*link) *link=s->next;
    SDL_GL_DeleteContext(s->context); SDL_DestroyWindow(s->window);
    if(!surfaces) SDL_StopTextInput();
    SDL_QuitSubSystem(SDL_INIT_VIDEO); free(s);
}
