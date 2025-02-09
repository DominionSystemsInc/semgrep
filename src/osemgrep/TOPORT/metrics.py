class MetricsJsonEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.astimezone().isoformat()

        if isinstance(obj, uuid.UUID):
            return str(obj)

        if isinstance(obj, set):
            return list(sorted(obj))

        return super().default(obj)


def suppress_errors(func: Callable[..., None]) -> Callable[..., None]:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.debug(f"Error in {func.__name__}: {e}")
            return None

    return wrapper


class Metrics:
    def __attrs_post_init__(self) -> None:
        self.payload["started_at"] = datetime.now()
        self.payload["environment"]["version"] = __VERSION__
        self.payload["environment"]["ci"] = os.getenv("CI")
        self.payload["event_id"] = uuid.uuid4()

    @property
    def is_using_registry(self) -> bool:
        return self._is_using_registry

    @is_using_registry.setter
    def is_using_registry(self, value: bool) -> None:
        if self.is_using_registry is False and value is True:
            logger.info(
                f"Semgrep rule registry URL is {os.environ.get('SEMGREP_URL', 'https://semgrep.dev/registry')}."
            )

        self._is_using_registry = value

    @suppress_errors
    def add_project_url(self, project_url: Optional[str]) -> None:
        """
        Standardizes url then hashes
        """
        if project_url is None:
            self.payload["environment"]["projectHash"] = None
            return

        try:
            parsed_url = urlparse(project_url)
            if parsed_url.scheme == "https":
                # Remove optional username/password from project_url
                sanitized_url = f"{parsed_url.hostname}{parsed_url.path}"
            else:
                # For now don't do anything special with other git-url formats
                sanitized_url = project_url
        except ValueError:
            logger.debug(f"Failed to parse url {project_url}")
            sanitized_url = project_url

        m = hashlib.sha256(sanitized_url.encode())
        self.payload["environment"]["projectHash"] = cast(Sha256Hash, m.hexdigest())

    @suppress_errors
    def add_configs(self, configs: Sequence[str]) -> None:
        """
        Assumes configs is list of arguments passed to semgrep using --config
        """
        m = hashlib.sha256()
        for c in configs:
            m.update(c.encode())
        self.payload["environment"]["configNamesHash"] = cast(Sha256Hash, m.hexdigest())

    @suppress_errors
    def add_rules(self, rules: Sequence[Rule], profiling_data: ProfilingData) -> None:
        rules = sorted(rules, key=lambda r: r.full_hash)
        m = hashlib.sha256()
        for rule in rules:
            m.update(rule.full_hash.encode())
        self.payload["environment"]["rulesHash"] = cast(Sha256Hash, m.hexdigest())

        self.payload["performance"]["numRules"] = len(rules)
        self.payload["performance"]["ruleStats"] = [
            {
                "ruleHash": rule.full_hash,
                "matchTime": profiling_data.get_rule_match_time(rule),
                "bytesScanned": profiling_data.get_rule_bytes_scanned(rule),
            }
            for rule in rules
        ]

    @suppress_errors
    def add_findings(self, findings: FilteredMatches) -> None:
        self.payload["value"]["ruleHashesWithFindings"] = {
            r.full_hash: len(f) for r, f in findings.kept.items()
        }
        self.payload["value"]["numFindings"] = sum(
            len(v) for v in findings.kept.values()
        )
        self.payload["value"]["numIgnored"] = sum(
            len(v) for v in findings.removed.values()
        )

    @suppress_errors
    def add_targets(self, targets: Set[Path], profiling_data: ProfilingData) -> None:
        self.payload["performance"]["fileStats"] = [
            {
                "size": target.stat().st_size,
                "numTimesScanned": profiling_data.get_file_num_times_scanned(target),
                "parseTime": profiling_data.get_file_parse_time(target),
                "matchTime": profiling_data.get_file_match_time(target),
                "runTime": profiling_data.get_file_run_time(target),
            }
            for target in targets
        ]

        total_bytes_scanned = sum(t.stat().st_size for t in targets)
        self.payload["performance"]["totalBytesScanned"] = total_bytes_scanned
        self.payload["performance"]["numTargets"] = len(targets)

    @suppress_errors
    def add_errors(self, errors: List[SemgrepError]) -> None:
        self.payload["errors"]["errors"] = [e.semgrep_error_type() for e in errors]

    @suppress_errors
    def add_profiling(self, profiler: ProfileManager) -> None:
        self.payload["performance"]["profilingTimes"] = profiler.dump_stats()

    @suppress_errors
    def add_token(self, token: Optional[str]) -> None:
        self.payload["environment"]["isAuthenticated"] = bool(token)

    @suppress_errors
    def add_exit_code(self, exit_code: int) -> None:
        self.payload["errors"]["returnCode"] = exit_code

    @suppress_errors
    def add_version(self, version: str) -> None:
        self.payload["environment"]["version"] = version

    @suppress_errors
    def add_feature(self, category: LiteralString, name: str) -> None:
        self.payload["value"]["features"].add(f"{category}/{name}")

    @suppress_errors
    def add_registry_url(self, url_string: str) -> None:
        path = urlparse(url_string).path
        parts = path.lstrip("/").split("/")
        if len(parts) != 2:
            return  # not a simple registry shorthand

        prefix, name = parts

        if prefix == "r":
            # we want to avoid reporting specific rules, so we do this mapping:
            # r/python -> "python"
            # r/python.flask -> "python."
            # r/python.correctness.lang => "python.."
            query_parts = name.split(".")
            dot_count = len(query_parts) - 1
            self.add_feature("registry-query", query_parts[0] + dot_count * ".")
        if prefix == "p":
            self.add_feature("ruleset", name)

    @suppress_errors
    def add_fix_rate(
        self, lower_limits: Dict[str, int], upper_limits: Dict[str, int]
    ) -> None:
        logger.debug(f"Adding fix rate: {lower_limits} {upper_limits}")
        self.payload["fix_rate"]["lowerLimits"] = lower_limits
        self.payload["fix_rate"]["upperLimits"] = upper_limits

    @suppress_errors
    def add_parse_rates(self, parse_rates: ParsingData) -> None:
        """
        Adds parse rates, grouped by language
        """
        self.payload["parse_rate"] = {
            lang: ParseStatSchema(
                targets_parsed=data.num_targets - data.targets_with_errors,
                num_targets=data.num_targets,
                bytes_parsed=data.num_bytes - data.error_bytes,
                num_bytes=data.num_bytes,
            )
            for (lang, data) in parse_rates.get_errors_by_lang().items()
        }

    def as_json(self) -> str:
        return json.dumps(
            self.payload, indent=2, sort_keys=True, cls=MetricsJsonEncoder
        )

    @property
    def is_enabled(self) -> bool:
        """
        Returns whether metrics should be sent.

        If metrics_state is:
          - auto, sends if using_registry
          - on, sends
          - off, doesn't send
        """
        if self.metrics_state == MetricsState.AUTO:
            return self.is_using_registry
        return self.metrics_state == MetricsState.ON

    @suppress_errors
    def gather_click_params(self) -> None:
        ctx = click.get_current_context()
        if ctx is None:
            return
        for param in ctx.params:
            source = ctx.get_parameter_source(param)
            if source == click.core.ParameterSource.COMMANDLINE:
                self.add_feature("cli-flag", param)
            if source == click.core.ParameterSource.ENVIRONMENT:
                self.add_feature("cli-envvar", param)
            if source == click.core.ParameterSource.PROMPT:
                self.add_feature("cli-prompt", param)

    @suppress_errors
    def send(self) -> None:
        """
        Send metrics to the metrics server.

        Will if is_enabled is True
        """
        state = get_state()
        logger.verbose(
            f"{'Sending' if self.is_enabled else 'Not sending'} pseudonymous metrics since metrics are configured to {self.metrics_state.name} and registry usage is {self.is_using_registry}"
        )

        if not self.is_enabled:
            return

        self.gather_click_params()
        self.payload["sent_at"] = datetime.now()
        self.payload["anonymous_user_id"] = state.settings.get("anonymous_user_id")

        r = requests.post(
            METRICS_ENDPOINT,
            data=self.as_json(),
            headers={
                "Content-Type": "application/json",
                "User-Agent": str(state.app_session.user_agent),
            },
            timeout=3,
        )
        r.raise_for_status()
