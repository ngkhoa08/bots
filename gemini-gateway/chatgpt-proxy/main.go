package main

import (
    "io"
    "net/http"
    "os"
    "strings"
)

func main() {
    port := os.Getenv("PORT")
    if port == "" { port = "10000" }
    secret := strings.TrimSpace(os.Getenv("CHATGPT_PATH_SECRET"))
    bearer := strings.TrimSpace(os.Getenv("PLUGIN_TOKEN"))

    http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
        if secret == "" || r.URL.Path != "/mcp/"+secret {
            http.NotFound(w, r)
            return
        }
        req, err := http.NewRequestWithContext(r.Context(), r.Method, "http://127.0.0.1:10001/mcp", r.Body)
        if err != nil { http.Error(w, "proxy request error", 500); return }
        for k, vv := range r.Header {
            for _, v := range vv { req.Header.Add(k, v) }
        }
        req.Header.Set("Authorization", "Bearer "+bearer)
        resp, err := http.DefaultClient.Do(req)
        if err != nil { http.Error(w, "gateway unavailable", 502); return }
        defer resp.Body.Close()
        for k, vv := range resp.Header {
            for _, v := range vv { w.Header().Add(k, v) }
        }
        w.WriteHeader(resp.StatusCode)
        _, _ = io.Copy(w, resp.Body)
    })

    _ = http.ListenAndServe(":"+port, nil)
}
