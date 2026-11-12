/** Registers the AG Grid / AG Charts community modules (MIT tier only). Imported once by the lazily loaded Dashboard. */
import { AllCommunityModule, ModuleRegistry } from "ag-grid-community";
import { AllCommunityModule as ChartsCommunity, ModuleRegistry as ChartsRegistry } from "ag-charts-community";

ModuleRegistry.registerModules([AllCommunityModule]);
ChartsRegistry.registerModules([ChartsCommunity]);
