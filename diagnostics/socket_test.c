/* Runs only inside the disposable VM. No external addresses or network needed. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <netinet/tcp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

#define MPTCP_PROTOCOL 262

static void die(const char *what) { perror(what); exit(1); }
static void deadline(int fd) {
    struct timeval tv = {5, 0};
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv))) die("rcv timeout");
    if (setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv))) die("snd timeout");
}

static void transfer(int family, int server_proto, int client_proto, int expect_brutal) {
    int listener = socket(family, SOCK_STREAM, server_proto);
    if (listener < 0) die("listener socket");
    deadline(listener);
    struct sockaddr_storage server = {0}, client_addr = {0};
    socklen_t length;
    if (family == AF_INET) {
        struct sockaddr_in *s = (void *)&server, *c = (void *)&client_addr;
        s->sin_family = c->sin_family = AF_INET;
        inet_pton(AF_INET, "192.0.2.1", &s->sin_addr);
        inet_pton(AF_INET, "192.0.2.2", &c->sin_addr);
        length = sizeof(*s);
    } else {
        struct sockaddr_in6 *s = (void *)&server, *c = (void *)&client_addr;
        s->sin6_family = c->sin6_family = AF_INET6;
        inet_pton(AF_INET6, "fd00::1", &s->sin6_addr);
        inet_pton(AF_INET6, "fd00::2", &c->sin6_addr);
        length = sizeof(*s);
    }
    if (bind(listener, (void *)&server, length) || listen(listener, 8)) die("listen");
    if (getsockname(listener, (void *)&server, &length)) die("getsockname");
    pid_t child = fork();
    if (child < 0) die("fork");
    if (!child) {
        close(listener);
        int ns = open("/run/netns/client", O_RDONLY);
        if (ns < 0 || setns(ns, CLONE_NEWNET)) die("client netns");
        close(ns);
        int fd = socket(family, SOCK_STREAM, client_proto);
        if (fd < 0) die("client socket");
        deadline(fd);
        if (bind(fd, (void *)&client_addr, length)) die("client bind");
        if (connect(fd, (void *)&server, length)) die("connect");
        char data[4096];
        size_t total = 0;
        while (total < 262144) {
            ssize_t n = read(fd, data, sizeof(data));
            if (n <= 0) die("client read");
            for (ssize_t i = 0; i < n; ++i) assert(data[i] == 'x');
            total += (size_t)n;
        }
        if (write(fd, "OK", 2) != 2) die("client ack");
        close(fd);
        _exit(0);
    }
    puts("ACCEPT_BEGIN"); fflush(stdout);
    int fd = accept(listener, NULL, NULL);
    if (fd < 0) die("accept");
    deadline(fd);
    char cc[32] = {0}; socklen_t cc_len = sizeof(cc);
    if (getsockopt(fd, IPPROTO_TCP, TCP_CONGESTION, cc, &cc_len)) die("get cc");
    printf("ACCEPT_OK family=%d server=%d client=%d cc=%s\n", family, server_proto, client_proto, cc);
    fflush(stdout);
    assert((strcmp(cc, "brutal") == 0) == expect_brutal);
    struct tcp_info info; socklen_t info_len = sizeof(info);
    if (getsockopt(fd, IPPROTO_TCP, TCP_INFO, &info, &info_len)) die("TCP_INFO");
    char block[4096]; memset(block, 'x', sizeof(block));
    for (size_t sent = 0; sent < 262144;) {
        ssize_t n = write(fd, block, sizeof(block));
        if (n <= 0) die("server write");
        sent += (size_t)n;
    }
    char reply[2];
    if (recv(fd, reply, 2, MSG_WAITALL) != 2 || memcmp(reply, "OK", 2)) die("server ack");
    close(fd); close(listener);
    int status;
    if (waitpid(child, &status, 0) != child || !WIFEXITED(status) || WEXITSTATUS(status)) die("child");
}

int main(int argc, char **argv) {
    int family = argc > 1 && !strcmp(argv[1], "6") ? AF_INET6 : AF_INET;
    int expect = argc > 2 ? atoi(argv[2]) : 1;
    int loops = argc > 3 ? atoi(argv[3]) : 1;
    int tcp_only = argc > 4 && !strcmp(argv[4], "tcp-only");
    for (int i = 0; i < loops; ++i) {
        if (!tcp_only) {
            transfer(family, MPTCP_PROTOCOL, IPPROTO_TCP, expect);
            transfer(family, MPTCP_PROTOCOL, MPTCP_PROTOCOL, expect);
        }
        transfer(family, IPPROTO_TCP, IPPROTO_TCP, expect);
    }
    puts("SOCKET_TEST_PASS");
    return 0;
}
