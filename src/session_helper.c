/* SPDX-License-Identifier: MIT */
/* Own waitpid in a separate process: Bash may reap any of its direct children.
 * FD 3 = slave, 4 = status writer, 5 = lifetime pipe, 6 = master duplicate.
 * The command receives only its terminal on descriptors 0, 1 and 2. */
#define _GNU_SOURCE
#include "session.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

static uint64_t now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000 + (uint64_t)ts.tv_nsec / 1000000;
}
static void report(int type, int value) {
    BtMessage message = {type, value};
    ssize_t n;
    do { n = write(4, &message, sizeof(message)); } while (n < 0 && errno == EINTR);
}
static void report_exec_error(int fd, int error) {
    ssize_t written;
    do { written = write(fd, &error, sizeof(error)); } while (written < 0 && errno == EINTR);
}
static volatile sig_atomic_t stop_requested;
static void request_stop(int sig) { (void)sig; stop_requested=1; }

/* Hold a pidfd across the parent check so a recycled numeric PID cannot
 * redirect a signal. The subreaper adopts descendants as their parents exit. */
static void signal_child(long id, int sig) {
    if(id<=1 || id>INT_MAX) return;
    int pidfd=(int)syscall(SYS_pidfd_open,(pid_t)id,0);
    if(pidfd<0) return;
    char path[96];
    snprintf(path,sizeof(path),"/proc/%ld/status",id);
    FILE *status=fopen(path,"re");
    bool owned=false;
    if(status) {
        char line[256]; long parent;
        while(fgets(line,sizeof(line),status))
            if(sscanf(line,"PPid: %ld",&parent)==1) { owned=parent==(long)getpid(); break; }
        fclose(status);
    }
    if(owned) (void)syscall(SYS_pidfd_send_signal,pidfd,sig,NULL,0);
    close(pidfd);
}
static int scan_proc_children(int sig) {
    DIR *directory=opendir("/proc");
    if(!directory) return -1;
    struct dirent *entry;
    for(;;) {
        errno=0;
        entry=readdir(directory);
        if(!entry) {
            int saved=errno;
            if(closedir(directory)<0) return -1;
            errno=saved;
            return saved ? -1 : 0;
        }
        char *end;
        long id=strtol(entry->d_name,&end,10);
        if(end!=entry->d_name && !*end) signal_child(id,sig);
    }
}
/* Prefer the kernel's direct-child list; some Linux kernels omit it. */
static int signal_children(int sig) {
    char path[96],buffer[65536];
    snprintf(path,sizeof(path),"/proc/self/task/%ld/children",(long)getpid());
    int fd=open(path,O_RDONLY|O_CLOEXEC);
    if(fd<0) return errno==ENOENT ? scan_proc_children(sig) : -1;
    ssize_t n;
    do { n=read(fd,buffer,sizeof(buffer)-1); } while(n<0 && errno==EINTR);
    int saved=errno; close(fd); errno=saved;
    if(n<0) return -1;
    buffer[n]=0;
    char *next=buffer;
    while(*next) {
        char *end;
        long id=strtol(next,&end,10);
        if(end==next) break;
        next=end;
        signal_child(id,sig);
    }
    return 0;
}
static bool reap_children(pid_t child, bool *original_reaped) {
    for(;;) {
        int status;
        pid_t waited=waitpid(-1,&status,WNOHANG);
        if(waited==0) return true;
        if(waited<0) {
            if(errno==EINTR) continue;
            return errno!=ECHILD;
        }
        if(waited==child && !*original_reaped) {
            int code=WIFEXITED(status)?WEXITSTATUS(status):WIFSIGNALED(status)?128+WTERMSIG(status):1;
            report(BT_EXIT,code);
            *original_reaped=true;
        }
    }
}

int main(int argc, char **argv) {
    if (argc < 2) return 2;
    signal(SIGPIPE, SIG_IGN);
    signal(SIGCHLD, SIG_DFL);
    struct sigaction stop={.sa_handler=request_stop};
    sigemptyset(&stop.sa_mask);
    sigaction(SIGHUP,&stop,NULL); sigaction(SIGTERM,&stop,NULL); sigaction(SIGINT,&stop,NULL);
    for (int fd = 3; fd <= 6; ++fd) fcntl(fd, F_SETFD, FD_CLOEXEC);
    if(prctl(PR_SET_CHILD_SUBREAPER,1)<0) { report(BT_EXEC_ERROR,errno); return 1; }
    int identity=(int)syscall(SYS_pidfd_open,getpid(),0);
    if(identity<0) { report(BT_EXEC_ERROR,errno); return 1; }
    close(identity);
    if(signal_children(0)<0) { report(BT_EXEC_ERROR,errno); return 1; }
    setenv("TERM", "xterm-256color", 1);
    setenv("COLORTERM", "truecolor", 1);
    setenv("TERM_PROGRAM", "batty", 1);
    unsetenv("COLUMNS");
    unsetenv("LINES");
    unsetenv("BATTY_RECOVERY_FILE");
    int exec_pipe[2];
    if (pipe2(exec_pipe, O_CLOEXEC) < 0) { report(BT_EXEC_ERROR, errno); return 1; }
    pid_t child = fork();
    if (child < 0) { report(BT_EXEC_ERROR, errno); return 1; }
    if (child == 0) {
        close(exec_pipe[0]);
        for (int sig = 1; sig < NSIG; ++sig) signal(sig, SIG_DFL);
        sigset_t empty;
        sigemptyset(&empty);
        sigprocmask(SIG_SETMASK, &empty, NULL);
        if (setsid() < 0 || ioctl(3, TIOCSCTTY, 0) < 0 ||
            dup2(3, 0) < 0 || dup2(3, 1) < 0 || dup2(3, 2) < 0) {
            int saved = errno;
            report_exec_error(exec_pipe[1], saved);
            _exit(126);
        }
        if (dup3(exec_pipe[1], 3, O_CLOEXEC) < 0) {
            int saved=errno; report_exec_error(exec_pipe[1],saved); _exit(126);
        }
        /* The spawn file actions closed every inherited descriptor above 6;
         * the exec handshake is the only descriptor opened afterwards. */
        for(int fd=4;fd<=exec_pipe[1];++fd) close(fd);
        execvp(argv[1], argv + 1);
        int saved = errno;
        report_exec_error(3, saved);
        _exit(saved == ENOENT ? 127 : 126);
    }
    close(3);
    close(exec_pipe[1]);
    fcntl(exec_pipe[0], F_SETFL, O_NONBLOCK);
    report(BT_STARTED, (int)child);
    bool reaped = false, closing = false, exec_done = false;
    uint64_t close_time = 0;
    int stage = 0;
    for (;;) {
        struct pollfd fds[] = {{closing ? -1 : 5, POLLIN | POLLHUP, 0}, {exec_done ? -1 : exec_pipe[0], POLLIN | POLLHUP, 0}};
        int rc = poll(fds, 2, 20);
        if (rc < 0 && errno != EINTR) closing = true;
        if (stop_requested) closing = true;
        if (fds[0].revents && !closing) {
            closing = true;
        }
        if (!exec_done && fds[1].revents) {
            int err = 0;
            ssize_t n = read(exec_pipe[0], &err, sizeof(err));
            if (n == 0 || n == sizeof(err)) {
                report(n == 0 ? BT_READY : BT_EXEC_ERROR, err);
                close(exec_pipe[0]);
                exec_done = true;
            }
        }
        if (closing && stage == 0) {
            close_time = now_ms();
            stage = 1;
        }
        bool descendants=reap_children(child,&reaped);
        if(closing && descendants) {
            uint64_t elapsed=now_ms()-close_time;
            int sig=elapsed>=400?SIGKILL:elapsed>=200?SIGTERM:SIGHUP;
            (void)signal_children(sig);
        }
        /* Stay alive after normal command exit until the host closes the handle,
         * so the lifetime pipe still controls cleanup of foreground descendants. */
        if (closing && !descendants) break;
    }
    if(!exec_done) close(exec_pipe[0]);
    close(6);
    return 0;
}
