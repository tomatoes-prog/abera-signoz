package telemetrymetadata

import aberanamespace "github.com/SigNoz/signoz/pkg/abera/namespace"

import otelconst "github.com/SigNoz/signoz-otel-collector/constants"

const (
	AttributesMetadataTableName      = "distributed_attributes_metadata"
	AttributesMetadataLocalTableName = "attributes_metadata"
	ColumnEvolutionMetadataTableName = "distributed_column_evolution_metadata"
	FieldKeysTable                   = otelconst.DistributedFieldKeysTable
	// Column Evolution table stores promoted paths as (signal, column_name, field_context, field_name); see signoz-otel-collector metadata_migrations.
	PromotedPathsTableName = "distributed_column_evolution_metadata"
	SkipIndexTableName     = "system.data_skipping_indices"
)

var DBName = aberanamespace.Resolve("signoz_metadata")
