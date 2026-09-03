package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	maxMCPBody    = 2 << 20 // 2 MiB: keeps memory bounded; large files should use HTTPS URLs.
	maxGeminiBody = 4 << 20
	maxInlineB64  = 1400000 // ~1 MiB raw data after base64 decoding.
)

type keyState struct {
	Value    string
	Cooldown time.Time
	Disabled bool
}

type keyPool struct {
	mu   sync.Mutex
	keys []keyState
	next int
}

func newPool(raw string) *keyPool {
	raw = strings.NewReplacer("\r", "\n", ";", ",", "\n", ",").Replace(raw)
	seen := map[string]bool{}
	p := &keyPool{}
	for _, s := range strings.Split(raw, ",") {
		s = strings.TrimSpace(s)
		if s != "" && !seen[s] {
			seen[s] = true
			p.keys = append(p.keys, keyState{Value: s})
		}
	}
	return p
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
		if !k.Disabled && !now.Before(k.Cooldown) {
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
	return p.keys[i].Value
}

func (p *keyPool) cooldown(i int, d time.Duration) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.keys[i].Cooldown = time.Now().Add(d)
}

func (p *keyPool) disable(i int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.keys[i].Disabled = true
}

type app struct {
	pool   *keyPool
	model  string
	secret string
	client *http.Client
	gate   chan struct{} // one Gemini request at a time keeps peak RAM low.
}

type rpcReq struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type fileArg struct {
	URI      string `json:"uri,omitempty"`
	MIMEType string `json:"mime_type"`
	DataB64  string `json:"data_base64,omitempty"`
}

type chatArgs struct {
	Prompt string    `json:"prompt"`
	System string    `json:"system_instruction,omitempty"`
	Files  []fileArg `json:"files,omitempty"`
}

type interactionResponse struct {
	ID     string `json:"id"`
	Model  string `json:"model"`
	Status string `json:"status"`
	Steps  []struct {
		Type    string `json:"type"`
		Content []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
	} `json:"steps"`
	Usage struct {
		TotalInputTokens   int `json:"total_input_tokens"`
		TotalOutputTokens  int `json:"total_output_tokens"`
		TotalThoughtTokens int `json:"total_thought_tokens"`
		TotalTokens        int `json:"total_tokens"`
	} `json:"usage"`
}

func main() {
	transport := &http.Transport{
		MaxIdleConns:        2,
		MaxIdleConnsPerHost: 2,
		IdleConnTimeout:     30 * time.Second,
		ForceAttemptHTTP2:   true,
	}
	a := &app{
		pool:   newPool(os.Getenv("GEMINI_KEYS")),
		model:  env("DEFAULT_MODEL", "gemini-3.8-flash"),
		secret: strings.TrimSpace(os.Getenv("CHATGPT_PATH_SECRET")),
		client: &http.Client{Timeout: 240 * time.Second, Transport: transport},
		gate:   make(chan struct{}, 1),
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{"ok": true, "model": a.model})
	})
	mux.HandleFunc("/", a.mcp)

	server := &http.Server{
		Addr:              ":" + env("PORT", "10000"),
		Handler:           mux,
		ReadHeaderTimeout: 8 * time.Second,
		IdleTimeout:       45 * time.Second,
		WriteTimeout:      250 * time.Second,
	}
	_ = server.ListenAndServe()
}

func env(k, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return fallback
}

func (a *app) mcp(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	if a.secret == "" || r.URL.Path != "/mcp/"+a.secret {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	r.Body = http.MaxBytesReader(w, r.Body, maxMCPBody)
	var q rpcReq
	if err := json.NewDecoder(r.Body).Decode(&q); err != nil {
		rpcErr(w, nil, -32700, "parse error or request too large")
		return
	}

	switch q.Method {
	case "initialize":
		rpcResult(w, q.ID, map[string]any{
			"protocolVersion": "2025-06-18",
			"capabilities":    map[string]any{"tools": map[string]any{"listChanged": false}},
			"serverInfo":      map[string]any{"name": "gemini-3.8-flash-gateway", "version": "1.2.0"},
			"instructions": "Use gemini_chat for Gemini 3.8 Flash. Pass the complete request in prompt. " +
				"For files, prefer a directly accessible HTTPS URL plus the correct MIME type; this lets Gemini fetch the file without loading it into this MCP server. " +
				"If no usable file URL exists, pass the document's extracted text in prompt. Never invent a file URL.",
		})
	case "notifications/initialized":
		w.WriteHeader(http.StatusAccepted)
	case "ping":
		rpcResult(w, q.ID, map[string]any{})
	case "tools/list":
		rpcResult(w, q.ID, map[string]any{"tools": []any{geminiTool()}})
	case "tools/call":
		a.callTool(w, r, q)
	default:
		rpcErr(w, q.ID, -32601, "method not found")
	}
}

func geminiTool() map[string]any {
	return map[string]any{
		"name": "gemini_chat",
		"description": "Ask Gemini 3.8 Flash using text and optional files. Supports PDFs, images, audio, video, JSON/CSV/HTML/RTF and other supported text formats. Prefer HTTPS file URLs to minimize server memory.",
		"inputSchema": map[string]any{
			"type": "object",
			"properties": map[string]any{
				"prompt": map[string]any{"type": "string", "description": "The complete request to answer."},
				"system_instruction": map[string]any{"type": "string", "description": "Optional response-style or constraint instruction."},
				"files": map[string]any{
					"type":        "array",
					"maxItems":    8,
					"description": "Optional media/documents. For large files use uri. data_base64 is only for small files (about 1 MiB raw each).",
					"items": map[string]any{
						"type": "object",
						"properties": map[string]any{
							"uri":         map[string]any{"type": "string", "description": "Public or signed HTTPS URL that Gemini can fetch directly."},
							"mime_type":   map[string]any{"type": "string", "description": "IANA MIME type, e.g. application/pdf, image/png, audio/mpeg, video/mp4, text/csv."},
							"data_base64": map[string]any{"type": "string", "description": "Base64 file bytes for a small inline file. Do not use together with uri."},
						},
						"required":             []string{"mime_type"},
						"additionalProperties": false,
					},
				},
			},
			"required":             []string{"prompt"},
			"additionalProperties": false,
		},
	}
}

func (a *app) callTool(w http.ResponseWriter, r *http.Request, q rpcReq) {
	var p struct {
		Name      string          `json:"name"`
		Arguments json.RawMessage `json:"arguments"`
	}
	if json.Unmarshal(q.Params, &p) != nil || p.Name != "gemini_chat" {
		rpcErr(w, q.ID, -32602, "invalid tool call")
		return
	}
	var in chatArgs
	if json.Unmarshal(p.Arguments, &in) != nil {
		rpcErr(w, q.ID, -32602, "invalid arguments")
		return
	}

	select {
	case a.gate <- struct{}{}:
		defer func() { <-a.gate }()
	case <-r.Context().Done():
		rpcResult(w, q.ID, toolError("request cancelled while waiting"))
		return
	}

	text, meta, err := a.generate(r.Context(), in)
	if err != nil {
		rpcResult(w, q.ID, toolError("Gemini request failed: "+err.Error()))
		return
	}
	rpcResult(w, q.ID, map[string]any{
		"content":           []any{map[string]any{"type": "text", "text": text}},
		"structuredContent": map[string]any{"answer": text, "meta": meta},
		"isError":           false,
	})
}

func toolError(msg string) map[string]any {
	return map[string]any{
		"content": []any{map[string]any{"type": "text", "text": msg}},
		"isError": true,
	}
}

func (a *app) generate(ctx context.Context, in chatArgs) (string, map[string]any, error) {
	in.Prompt = strings.TrimSpace(in.Prompt)
	if in.Prompt == "" {
		return "", nil, errors.New("prompt is required")
	}
	if len(in.Files) > 8 {
		return "", nil, errors.New("too many files; maximum is 8")
	}

	input := make([]any, 0, len(in.Files)+1)
	input = append(input, map[string]any{"type": "text", "text": in.Prompt})
	for _, f := range in.Files {
		part, err := filePart(f)
		if err != nil {
			return "", nil, err
		}
		input = append(input, part)
	}

	payload := map[string]any{
		"model": a.model,
		"store": false,
		"input": input,
		"generation_config": map[string]any{
			"thinking_level":   "medium",
			"max_output_tokens": 65536,
		},
	}
	if strings.TrimSpace(in.System) != "" {
		payload["system_instruction"] = strings.TrimSpace(in.System)
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return "", nil, err
	}
	if len(body) > maxMCPBody {
		return "", nil, errors.New("inline payload is too large; use HTTPS file URLs instead")
	}

	ids := a.pool.candidates()
	if len(ids) == 0 {
		return "", nil, errors.New("no Gemini key available")
	}

	var last error
	for _, i := range ids {
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, "https://generativelanguage.googleapis.com/v1beta/interactions", bytes.NewReader(body))
		if err != nil {
			return "", nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("x-goog-api-key", a.pool.value(i))
		req.Header.Set("Api-Revision", "2026-05-20")

		resp, err := a.client.Do(req)
		if err != nil {
			last = err
			a.pool.cooldown(i, 10*time.Second)
			continue
		}
		raw, readErr := io.ReadAll(io.LimitReader(resp.Body, maxGeminiBody))
		resp.Body.Close()
		if readErr != nil {
			last = readErr
			continue
		}

		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			text, meta, err := extractInteraction(raw)
			if err == nil {
				return text, meta, nil
			}
			last = err
			continue
		}

		last = fmt.Errorf("Gemini HTTP %d: %s", resp.StatusCode, compactError(raw))
		switch {
		case resp.StatusCode == http.StatusTooManyRequests:
			a.pool.cooldown(i, retryAfter(resp.Header.Get("Retry-After"), 60*time.Second))
			continue
		case resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden:
			a.pool.disable(i)
			continue
		case resp.StatusCode == http.StatusRequestTimeout || resp.StatusCode >= 500:
			a.pool.cooldown(i, 15*time.Second)
			continue
		default:
			return "", nil, last
		}
	}
	if last == nil {
		last = errors.New("all Gemini keys failed")
	}
	return "", nil, last
}

func filePart(f fileArg) (map[string]any, error) {
	mime := strings.ToLower(strings.TrimSpace(f.MIMEType))
	if mime == "" {
		return nil, errors.New("each file requires mime_type")
	}
	kind, ok := inputKind(mime)
	if !ok {
		return nil, fmt.Errorf("unsupported mime_type: %s", mime)
	}
	uri := strings.TrimSpace(f.URI)
	data := strings.TrimSpace(f.DataB64)
	if (uri == "") == (data == "") {
		return nil, errors.New("each file must contain exactly one of uri or data_base64")
	}
	part := map[string]any{"type": kind, "mime_type": mime}
	if uri != "" {
		if !strings.HasPrefix(strings.ToLower(uri), "https://") {
			return nil, errors.New("file uri must use HTTPS")
		}
		part["uri"] = uri
		return part, nil
	}
	if len(data) > maxInlineB64 {
		return nil, errors.New("inline file is too large; use an HTTPS file URL instead")
	}
	part["data"] = data
	return part, nil
}

func inputKind(mime string) (string, bool) {
	switch {
	case strings.HasPrefix(mime, "image/"):
		return "image", true
	case strings.HasPrefix(mime, "audio/"):
		return "audio", true
	case strings.HasPrefix(mime, "video/"):
		return "video", true
	case strings.HasPrefix(mime, "text/"):
		return "document", true
	case mime == "application/pdf", mime == "application/json", mime == "application/rtf",
		mime == "application/x-javascript", mime == "application/x-typescript", mime == "application/x-python-code",
		mime == "application/x-ipynb+json":
		return "document", true
	default:
		return "", false
	}
}

func extractInteraction(raw []byte) (string, map[string]any, error) {
	var x interactionResponse
	if err := json.Unmarshal(raw, &x); err != nil {
		return "", nil, errors.New("invalid Gemini response")
	}
	var b strings.Builder
	for _, step := range x.Steps {
		if step.Type != "model_output" {
			continue
		}
		for _, c := range step.Content {
			if c.Type == "text" && c.Text != "" {
				b.WriteString(c.Text)
			}
		}
	}
	text := strings.TrimSpace(b.String())
	meta := map[string]any{
		"model":           x.Model,
		"status":          x.Status,
		"input_tokens":    x.Usage.TotalInputTokens,
		"output_tokens":   x.Usage.TotalOutputTokens,
		"thinking_tokens": x.Usage.TotalThoughtTokens,
		"total_tokens":    x.Usage.TotalTokens,
		"files":           0,
	}
	if x.Status == "incomplete" {
		return "", meta, errors.New("Gemini returned an incomplete result")
	}
	if x.Status != "" && x.Status != "completed" {
		return "", meta, fmt.Errorf("Gemini interaction status: %s", x.Status)
	}
	if text == "" {
		return "", meta, errors.New("empty Gemini response")
	}
	return text, meta, nil
}

func retryAfter(v string, fallback time.Duration) time.Duration {
	if n, err := strconv.Atoi(strings.TrimSpace(v)); err == nil && n > 0 && n <= 3600 {
		return time.Duration(n) * time.Second
	}
	return fallback
}

func compactError(raw []byte) string {
	var x struct {
		Error struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if json.Unmarshal(raw, &x) == nil && strings.TrimSpace(x.Error.Message) != "" {
		return strings.TrimSpace(x.Error.Message)
	}
	s := strings.TrimSpace(string(raw))
	if len(s) > 300 {
		s = s[:300]
	}
	return s
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func rpcResult(w http.ResponseWriter, id json.RawMessage, result any) {
	writeJSON(w, map[string]any{"jsonrpc": "2.0", "id": json.RawMessage(id), "result": result})
}

func rpcErr(w http.ResponseWriter, id json.RawMessage, code int, msg string) {
	writeJSON(w, map[string]any{
		"jsonrpc": "2.0",
		"id":      json.RawMessage(id),
		"error":   map[string]any{"code": code, "message": msg},
	})
}
