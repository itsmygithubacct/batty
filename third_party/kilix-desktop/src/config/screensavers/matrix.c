/*
 * matrix.c — efficient terminal "digital rain" (The Matrix screensaver).
 *
 * Pure ANSI + 24-bit colour, no ncurses, no dependencies. Each column is a
 * raindrop: a bright head that falls and leaves a green trail fading to black.
 *
 * Efficiency:
 *   - one write() per frame (the whole frame is batched into a buffer);
 *   - brightness is quantized to a few colour levels, and each frame only the
 *     cells whose (level, glyph) actually changed are redrawn — so a mostly
 *     static screen emits almost nothing;
 *   - the frame is wrapped in a synchronized update (CSI ?2026h/l) so a GPU
 *     terminal (kitty/kilix) paints it atomically, with no tearing;
 *   - autowrap is disabled so writing the last column never scrolls.
 *
 * Build: cc -O2 -o matrix matrix.c
 * Run:   ./matrix            (press q or Ctrl-C to quit; resizes live)
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <termios.h>
#include <signal.h>
#include <time.h>
#include <sys/ioctl.h>

#define NLEVELS 16          /* colour quantization: head + green gradient */
#define FPS     30

/* glyph set: half-width katakana (each width 1) + digits + a few symbols */
static const char *GLYPHS[] = {
    "ｱ","ｲ","ｳ","ｴ","ｵ","ｶ","ｷ","ｸ","ｹ","ｺ","ｻ","ｼ","ｽ","ｾ","ｿ",
    "ﾀ","ﾁ","ﾂ","ﾃ","ﾄ","ﾅ","ﾆ","ﾇ","ﾈ","ﾉ","ﾊ","ﾋ","ﾌ","ﾍ","ﾎ",
    "ﾏ","ﾐ","ﾑ","ﾒ","ﾓ","ﾔ","ﾕ","ﾖ","ﾗ","ﾘ","ﾙ","ﾚ","ﾛ","ﾜ","ﾝ","ｦ",
    "0","1","2","3","4","5","6","7","8","9",
    "=","+","*",":","|","<",">","ﾘ",
};
static const int NGLYPH = (int)(sizeof(GLYPHS)/sizeof(GLYPHS[0]));

static struct termios g_tio;
static volatile sig_atomic_t g_stop = 0, g_winch = 1;
static void on_stop(int s){ (void)s; g_stop = 1; }
static void on_winch(int s){ (void)s; g_winch = 1; }

static int  R, C;                         /* rows, cols */
static int *bri, *gl;                     /* grid: brightness 0..255, glyph idx */
static int *plvl, *pgl;                    /* last-rendered level/glyph (for diff) */
static int *dhead, *dfade, *dspd, *dtick;  /* per-column drop state */
static char *ob; static size_t obcap;      /* output batch buffer */

static void restore(void){
    fputs("\x1b[?25h\x1b[0m\x1b[?7h\x1b[?1049l", stdout);
    fflush(stdout);
    tcsetattr(STDIN_FILENO, TCSANOW, &g_tio);
}

static void spawn(int c){                  /* (re)start a column's drop above the top */
    dhead[c] = -(rand() % (R + 4));
    int len  = 5 + rand() % 18;            /* trail length -> fade rate */
    dfade[c] = 256 / len + 1;
    dspd[c]  = 1 + rand() % 3;             /* frames per downward step */
    dtick[c] = rand() % dspd[c];
}

static void resize(void){
    struct winsize ws;
    if (ioctl(STDOUT_FILENO, TIOCGWINSZ, &ws) == 0 && ws.ws_row) { R = ws.ws_row; C = ws.ws_col; }
    else { R = 24; C = 80; }
    free(bri); free(gl); free(plvl); free(pgl);
    free(dhead); free(dfade); free(dspd); free(dtick); free(ob);
    int n = R * C;
    bri  = calloc(n, sizeof(int));
    gl   = calloc(n, sizeof(int));
    plvl = malloc(n * sizeof(int));
    pgl  = malloc(n * sizeof(int));
    for (int i = 0; i < n; i++) { plvl[i] = -1; pgl[i] = -1; }   /* -1 = cell is off */
    dhead = malloc(C*sizeof(int)); dfade = malloc(C*sizeof(int));
    dspd  = malloc(C*sizeof(int)); dtick = malloc(C*sizeof(int));
    for (int c = 0; c < C; c++) spawn(c);
    obcap = (size_t)n * 40 + 64;           /* worst case: every cell redrawn */
    ob = malloc(obcap);
    fputs("\x1b[2J", stdout); fflush(stdout);
}

int main(void){
    srand((unsigned)time(NULL) ^ (unsigned)getpid());
    tcgetattr(STDIN_FILENO, &g_tio);
    struct termios raw = g_tio;
    raw.c_lflag &= ~(ICANON | ECHO);
    raw.c_cc[VMIN] = 0; raw.c_cc[VTIME] = 0;
    tcsetattr(STDIN_FILENO, TCSANOW, &raw);
    fputs("\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[2J", stdout); fflush(stdout);
    atexit(restore);
    signal(SIGINT, on_stop); signal(SIGTERM, on_stop); signal(SIGWINCH, on_winch);

    struct timespec ft = { 0, 1000000000L / FPS };
    while (!g_stop) {
        char ch;
        while (read(STDIN_FILENO, &ch, 1) == 1) if (ch == 'q' || ch == 3) g_stop = 1;
        if (g_winch) { g_winch = 0; resize(); }

        /* 1. fade every lit cell by its column's fade rate */
        for (int c = 0; c < C; c++) {
            int f = dfade[c];
            for (int r = 0; r < R; r++) { int i = r*C + c; if (bri[i] > 0) { bri[i] -= f; if (bri[i] < 0) bri[i] = 0; } }
        }
        /* 2. advance drops; light a fresh head where each lands */
        for (int c = 0; c < C; c++) {
            if (--dtick[c] > 0) continue;
            dtick[c] = dspd[c];
            int h = ++dhead[c];
            if (h >= 0 && h < R) { int i = h*C + c; bri[i] = 255; gl[i] = rand() % NGLYPH; }
            if (h > R + 2) spawn(c);
        }
        /* 3. flicker: mutate a few glyphs that are still lit */
        for (int k = 0; k < C/6 + 1; k++) { int i = (rand()%R)*C + (rand()%C); if (bri[i] > 0) gl[i] = rand() % NGLYPH; }

        /* 4. render only what changed, batched into one synchronized frame */
        char *p = ob;
        p += sprintf(p, "\x1b[?2026h");
        int cx = -1, cy = -1, clvl = -2;   /* virtual cursor + current colour level */
        for (int r = 0; r < R; r++) {
            for (int c = 0; c < C; c++) {
                int i = r*C + c;
                int lvl = bri[i] > 0 ? bri[i]*NLEVELS/256 : -1;
                if (lvl == plvl[i] && (lvl < 0 || gl[i] == pgl[i])) continue;
                if (cy != r || cx != c) p += sprintf(p, "\x1b[%d;%dH", r+1, c+1);
                if (lvl < 0) {
                    if (clvl != -2) { p += sprintf(p, "\x1b[0m"); clvl = -2; }
                    *p++ = ' ';
                } else {
                    if (lvl != clvl) {
                        if (lvl >= NLEVELS-1) p += sprintf(p, "\x1b[38;2;215;255;215m");
                        else { int g = 45 + lvl*195/(NLEVELS-2); p += sprintf(p, "\x1b[38;2;0;%d;0m", g); }
                        clvl = lvl;
                    }
                    for (const char *s = GLYPHS[gl[i]]; *s; ) *p++ = *s++;
                }
                plvl[i] = lvl; pgl[i] = gl[i];
                cy = r; cx = c + 1;         /* one width-1 glyph advances the cursor by 1 */
            }
        }
        p += sprintf(p, "\x1b[?2026l");
        (void)!write(STDOUT_FILENO, ob, (size_t)(p - ob));
        nanosleep(&ft, NULL);
    }
    return 0;                              /* restore() runs via atexit */
}
