package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

const maxBody = 1 << 20 // 1 MiB

type keyState struct {
	Value         string
	CooldownUntil time.Time
	Disabled      bool
}

type keyPool struct {
	mu   sync.Mutex
	keys []keyState
	next int
}

func newKeyPool(raw string) *keyPool {
	normalized := strings.NewReplacer("\r", "\n", ";", ",", "\n", ",").Replace(raw)
	seen := map[string]bool{}
	var keys []keyState
	for _, item := range strings.Split(normalized, ",") {
		k := strings.TrimSpace(item)
		if k != "" && !seen[k] {
			seen[k] = true
			keys = append(keys, keyState{Value: k})
		}
	}
	return &keyPool{keys: keys}
}

func (p *keyPool) size() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.keys)
}

func (p *keyPool) candidates() []int {
	p.mu.Lock()
	defer p.mu.Unlock()
	n := len(p.keys)
	if n == 0 {
		return nil
	}
	now := time.Now()
	out := make([]int, 0, n)
	for step := 0; step < n; step++ {
		i := (p.next + step) % n
		k := p.keys[i]
		if !k.Disabled && !now.Before(k.CooldownUntil) {
			out = append(out, i)
		}
	}
	if len(out) > 0 {
		p.next = (out[0] + 1) % n
	}
	return out
}

func (p *keyPool) value(i int) string {
	p.mu.Lock()
	defer p.mu.Unlock()
	if i < 0 || i >= len(p.keys) {
		return ""
	}
	return p.keys[i].Value
}

func (p *keyPool) cooldown(i int, d time.Duration) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if i >= 0 && i < len(p.keys) {
		p.keys[i].CooldownUntil = time.Now().Add(d)
	}
}

func (p *keyPool) disable(i int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if i >= 0 && i < len(p.keys) {
		p.keys[i].Disabled = true
	}
}

type chatArgs struct {
	Prompt          string   `json:"prompt"`
	Model           string   `json:"model,omitempty"`
	System          string   `json:"system_instruction,omitempty"`
	Temperature     *float64 `json:"temperature,omitempty"`
	MaxOutputTokens *int     `json:"max_output_tokens,omitempty"`
}

type app struct {
	pool         *keyPool
	token        string
	defaultModel string
	client       *http.Client
}

func main() {
	pool := newKeyPool(os.Getenv("GEMINI_KEYS"))
	a := &app{
		pool:         pool,
		token:        strings.TrimSpace(os.Getenv("PLUGIN_TOKEN")),
		defaultModel: envOr("DEFAULT_MODEL", "gemini-3.8-flash"),
		client:       &http.Client{Timeout: 90 * time.Second},
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /", a.root)
	mux.HandleFunc("GET /health", a.health)
	mux.Handle("POST /v1/chat", a.auth(http.HandlerFunc(a.restChat)))
	mux.Handle("POST /mcp", a.auth(http.HandlerFunc(a.mcp)))

	port := envOr("PORT", "10000")
	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           securityHeaders(mux),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	log.Printf("gemini-gateway listening on :%s with %d key(s)", port, pool.size())
	log.Fatal(srv.ListenAndServe())
}

func envOr(k, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return fallback
}

func securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Cache-Control", "no-store")
		next.ServeHTTP(w, r)
	})
}

func (a *app) auth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if a.token == "" {
			http.Error(w, "server authentication is not configured", http.StatusServiceUnavailable)
			return
		}
		got := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
		if got == "" {
			got = strings.TrimSpace(r.Header.Get("X-Plugin-Token"))
		}
		if got != a.token {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (a *app) root(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"name":   "Gemini Gateway MCP",
		"mcp":    "/mcp",
		"chat":   "/v1/chat",
		"health": "/health",
	})
}

func (a *app) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":              true,
		"configured_keys": a.pool.size(),
		"default_model":   a.defaultModel,
	})
}

func (a *app) restChat(w http.ResponseWriter, r *http.Request) {
	var in chatArgs
	if err := readJSON(r, &in); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	text, meta, err := a.generate(r.Context(), in)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]any{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"text": text, "meta": meta})
}

type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

func (a *app) mcp(w http.ResponseWriter, r *http.Request) {
	var req rpcRequest
	if err := readJSON(r, &req); err != nil {
		rpcError(w, nil, -32700, "parse error")
		return
	}
	if req.JSONRPC != "2.0" {
		rpcError(w, req.ID, -32600, "invalid request")
		return
	}

	switch req.Method {
	case "initialize":
		protocol := "2025-06-18"
		var p struct {
			ProtocolVersion string `json:"protocolVersion"`
		}
		_ = json.Unmarshal(req.Params, &p)
		if p.ProtocolVersion != "" {
			protocol = p.ProtocolVersion
		}
		rpcResult(w, req.ID, map[string]any{
			"protocolVersion": protocol,
			"capabilities":    map[string]any{"tools": map[string]any{"listChanged": false}},
			"serverInfo":      map[string]any{"name": "gemini-gateway", "version": "1.0.0"},
			"instructions":    "Use gemini_chat when the user explicitly wants a Gemini model response.",
		})
	case "notifications/initialized":
		w.WriteHeader(http.StatusAccepted)
	case "ping":
		rpcResult(w, req.ID, map[string]any{})
	case "tools/list":
		rpcResult(w, req.ID, map[string]any{"tools": []any{
			map[string]any{
				"name":        "gemini_chat",
				"description": "Send a text prompt to Google Gemini through the user's configured Gemini API keys.",
				"inputSchema": map[string]any{
					"type": "object",
					"properties": map[string]any{
						"prompt":             map[string]any{"type": "string", "description": "Prompt to send to Gemini."},
						"model":              map[string]any{"type": "string", "description": "Optional Gemini model name."},
						"system_instruction": map[string]any{"type": "string", "description": "Optional system instruction."},
						"temperature":        map[string]any{"type": "number", "minimum": 0, "maximum": 2},
						"max_output_tokens":  map[string]any{"type": "integer", "minimum": 1, "maximum": 65536},
					},
					"required":             []string{"prompt"},
					"additionalProperties": false,
				},
				"annotations": map[string]any{"readOnlyHint": true, "openWorldHint": true},
			},
		}})
	case "tools/call":
		var p struct {
			Name      string          `json:"name"`
			Arguments json.RawMessage `json:"arguments"`
		}
		if err := json.Unmarshal(req.Params, &p); err != nil || p.Name == "" {
			rpcError(w, req.ID, -32602, "invalid tool arguments")
			return
		}
		if p.Name != "gemini_chat" {
			rpcError(w, req.ID, -32601, "unknown tool")
			return
		}
		var in chatArgs
		if err := json.Unmarshal(p.Arguments, &in); err != nil {
			rpcError(w, req.ID, -32602, "invalid gemini_chat arguments")
			return
		}
		text, meta, err := a.generate(r.Context(), in)
		if err != nil {
			rpcResult(w, req.ID, map[string]any{
				"content": []any{map[string]any{"type": "text", "text": "Gemini request failed: " + err.Error()}},
				"isError": true,
			})
			return
		}
		rpcResult(w, req.ID, map[string]any{
			"content":           []any{map[string]any{"type": "text", "text": text}},
			"structuredContent": map[string]any{"text": text, "meta": meta},
			"isError":           false,
		})
	default:
		rpcError(w, req.ID, -32601, "method not found")
	}
}

func (a *app) generate(ctx context.Context, in chatArgs) (string, map[string]any, error) {
	in.Prompt = strings.TrimSpace(in.Prompt)
	if in.Prompt == "" {
		return "", nil, errors.New("prompt is required")
	}
	if a.pool.size() == 0 {
		return "", nil, errors.New("no Gemini API keys configured")
	}
	model := strings.TrimSpace(in.Model)
	if model == "" {
		model = a.defaultModel
	}
	if strings.ContainsAny(model, "/?#") {
		return "", nil, errors.New("invalid model name")
	}

	payload := map[string]any{
		"contents": []any{map[string]any{
			"role":  "user",
			"parts": []any{map[string]any{"text": in.Prompt}},
		}},
	}
	if strings.TrimSpace(in.System) != "" {
		payload["systemInstruction"] = map[string]any{"parts": []any{map[string]any{"text": strings.TrimSpace(in.System)}}}
	}
	gen := map[string]any{}
	if in.Temperature != nil {
		gen["temperature"] = *in.Temperature
	}
	if in.MaxOutputTokens != nil {
		gen["maxOutputTokens"] = *in.MaxOutputTokens
	}
	if len(gen) > 0 {
		payload["generationConfig"] = gen
	}
	body, _ := json.Marshal(payload)

	candidates := a.pool.candidates()
	if len(candidates) == 0 {
		return "", nil, errors.New("all Gemini keys are temporarily unavailable")
	}

	var lastErr error
	for attempt, idx := range candidates {
		key := a.pool.value(idx)
		endpoint := "https://generativelanguage.googleapis.com/v1beta/models/" + url.PathEscape(model) + ":generateContent"
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
		if err != nil {
			return "", nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("x-goog-api-key", key)
		req.Header.Set("User-Agent", "gemini-gateway/1.0")

		resp, err := a.client.Do(req)
		if err != nil {
			lastErr = err
			a.pool.cooldown(idx, 10*time.Second)
			continue
		}
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
		resp.Body.Close()

		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			text, err := extractGeminiText(raw)
			if err != nil {
				return "", nil, err
			}
			return text, map[string]any{"model": model, "attempt": attempt + 1}, nil
		}

		msg := geminiErrorMessage(raw)
		lastErr = fmt.Errorf("Gemini HTTP %d: %s", resp.StatusCode, msg)
		switch resp.StatusCode {
		case http.StatusTooManyRequests:
			a.pool.cooldown(idx, retryAfter(resp.Header.Get("Retry-After"), 60*time.Second))
			continue
		case http.StatusUnauthorized, http.StatusForbidden:
			a.pool.disable(idx)
			continue
		case http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout:
			a.pool.cooldown(idx, 15*time.Second)
			continue
		default:
			return "", nil, lastErr
		}
	}
	if lastErr == nil {
		lastErr = errors.New("no Gemini key succeeded")
	}
	return "", nil, lastErr
}

func extractGeminiText(raw []byte) (string, error) {
	var out struct {
		Candidates []struct {
			Content struct {
				Parts []struct {
					Text string `json:"text"`
				} `json:"parts"`
			} `json:"content"`
		} `json:"candidates"`
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return "", errors.New("invalid JSON response from Gemini")
	}
	var b strings.Builder
	if len(out.Candidates) > 0 {
		for _, p := range out.Candidates[0].Content.Parts {
			b.WriteString(p.Text)
		}
	}
	text := strings.TrimSpace(b.String())
	if text == "" {
		return "", errors.New("Gemini returned no text")
	}
	return text, nil
}

func geminiErrorMessage(raw []byte) string {
	var e struct {
		Error struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if json.Unmarshal(raw, &e) == nil && strings.TrimSpace(e.Error.Message) != "" {
		return strings.TrimSpace(e.Error.Message)
	}
	s := strings.TrimSpace(string(raw))
	if len(s) > 300 {
		s = s[:300]
	}
	if s == "" {
		s = "request failed"
	}
	return s
}

func retryAfter(v string, fallback time.Duration) time.Duration {
	if sec, err := strconv.Atoi(strings.TrimSpace(v)); err == nil && sec > 0 && sec <= 3600 {
		return time.Duration(sec) * time.Second
	}
	return fallback
}

func readJSON(r *http.Request, dst any) error {
	r.Body = http.MaxBytesReader(nil, r.Body, maxBody)
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return err
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func rpcResult(w http.ResponseWriter, id json.RawMessage, result any) {
	writeJSON(w, http.StatusOK, map[string]any{"jsonrpc": "2.0", "id": rawID(id), "result": result})
}

func rpcError(w http.ResponseWriter, id json.RawMessage, code int, message string) {
	writeJSON(w, http.StatusOK, map[string]any{"jsonrpc": "2.0", "id": rawID(id), "error": map[string]any{"code": code, "message": message}})
}

func rawID(id json.RawMessage) any {
	if len(id) == 0 {
		return nil
	}
	var v any
	if json.Unmarshal(id, &v) != nil {
		return nil
	}
	return v
}
